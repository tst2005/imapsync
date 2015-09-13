#!/usr/bin/env python

"""IMAP Incremental Backup Script"""
__version__ = "1.4a"
__author__ = "Rui Carmo (http://the.taoofmac.com)"
__copyright__ = "(C) 2006 Rui Carmo. Code under BSD License."
__contributors__ = "Bob Ippolito, Michael Leonhard"

# = Contributors =
# Michael Leonhard: LIST result parsing, SSL support, revamped argument processing,
#                   moved spinner into class, extended recv fix to Windows
# Bob Ippolito: fix for MemoryError on socket recv, http://python.org/sf/1092502
# Rui Carmo: original author, up to v1.2e

# THIS IS BETA SOFTWARE - USE AT YOUR OWN RISK
# For more information, see http://the.taoofmac.com/space/Projects/imapbackup
# or http://tamale.net/imapbackup

## [TsT's TODO] ##
# - add command line options :
#	--spinner|--no-spinner to enable/disable spinner
#	--debug|-d  to show debug message (or just -v|-q)
#	--message-id-warning|--no-message-id-warning
#       --quiet -q | -v --verbose
# - show size info
# - multiple pass (*inbox*/i ; Trash at the end)
# - see -120 new email = 120 deleted, what to do ? keep it or overwrite the mbox ?
# - show total size
# - --quota 45GB
# - show percent of use/quota
# - show warning if quota reached --quota-percent-warning 90%
# 

# bugfixed: -y remove current mbox but return, no write of downloaded data
# bugfixed: From format was no supported by mutt : fix to mutt/thunderbird compatible format


# = TODO =
# - Add proper exception handlers to scanFile() and downloadMessages()
# - Migrate mailbox usage from rfc822 module to email module
# - Investigate using the noseek mailbox/email option to improve speed
# - Use the email module to normalize downloaded messages
#   and add missing Message-Id
# - Test parseList() and its descendents on other imapds
# - Test bzip2 support
# - Add option to download only subscribed folders
# - Add regex option to filter folders
# - Use a single IMAP command to get Message-IDs
# - Use a single IMAP command to fetch the messages
# - Add option to turn off spinner.  Since sys.stdin.isatty() doesn't work on
#   Windows, redirecting output to a file results in junk output.
# - Patch Python's ssl module to do proper checking of certificate chain
# - Patch Python's ssl module to raise good exceptions
# - Submit patch of socket._fileobject.read
# - Improve imaplib module with LIST parsing code, submit patch
# DONE:
# v1.3c
# - Add SSL support
# - Support host:port
# - Cleaned up code using PyLint to identify problems
#   pylint -f html --indent-string="  " --max-line-length=90 imapbackup.py > report.html
import getpass, os, gc, sys, time, platform, getopt
import mailbox, imaplib, socket
import re, sha, gzip, bz2

def tweaksocket(server):
  server.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

config_message_id_warning=0
config_messahe_info_overwrite=1

def debugprint(msg):
  if len(msg) > 0:
    print("[I]: %s" % msg)
  else:
    print("")

def print_usage():
  """Prints usage, exits"""
  print "Usage: imapbackup [OPTIONS] -s HOST -u USERNAME [-p PASSWORD]"
  print " -a --append-to-mboxes     Append new messages to mbox files. (default)"
  print " -y --yes-overwrite-mboxes Overwite existing mbox files instead of appending."
  print " -n --compress=none        Use one plain mbox file for each folder. (default)"
  print " -z --compress=gzip        Use mbox.gz files.  Appending may be very slow."
  print " -b --compress=bzip2       Use mbox.bz2 files. Appending not supported: use -y."
  print " -e --ssl                  Use SSL.  Port defaults to 993."
  print " -k KEY --key=KEY          PEM private key file for SSL.  Specify cert, too."
  print " -c CERT --cert=CERT       PEM certificate chain for SSL.  Specify key, too."
  print "                           Python's SSL module doesn't check the cert chain."
  print " -s HOST --server=HOST     Address of server, port optional, eg. mail.com:143"
  print " -u USER --user=USER       Username to log into server"
  print " -p PASS --pass=PASS       Prompts for password if not specified."
  print "\nNOTE: mbox files are created in the current working directory."
  sys.exit(2)


class SkipFolderException(Exception):
  """Indicates aborting processing of current folder, continue with next folder."""
  pass

#def repeat_to_length(string_to_expand, length):
#  return (string_to_expand * ((length/len(string_to_expand))+1))[:length]
#def rep(s, m):
#  a, b = divmod(m, len(s))
#  return s * a + s[:b]

class Spinner:
  """Prints out message with cute spinner, indicating progress"""
  
  def __init__(self, message):
    """Spinner constructor"""
    self.glyphs = "|/-\\"
    self.pos = 0
    self.message = message
    sys.stdout.write(message)
    sys.stdout.flush()
    self.spin()
  
  def spin(self):
    """Rotate the spinner"""
    if sys.stdin.isatty():
      sys.stdout.write("\r" + self.message + " " + self.glyphs[self.pos])
      sys.stdout.flush()
      self.pos = (self.pos+1) % len(self.glyphs)

  def stop(self):
    """Erase the spinner from the screen"""
    if sys.stdin.isatty():
      #sys.stdout.write("\r" + self.message + "  ")
      #sys.stdout.write("\r" + self.message)
      sys.stdout.write("\r" + (" " * len(self.message)) + "  ")
      sys.stdout.write("\r")
      sys.stdout.flush()

def pretty_byte_count(num):
  """Converts integer into a human friendly count of bytes, eg: 12.243 MB"""
  if num == 1:
    return "%s B" % (num)
  elif num < 1024:
    return "%s B" % (num)
  elif num < 1048576:
    return "%.1f KB" % (num/1024.0)
  elif num < 1073741824:
    return "%.1f MB" % (num/1048576.0)
  elif num < 1099511627776:
    return "%.1f GB" % (num/1073741824.0)
  else:
    return "%.1f TB" % (num/1099511627776.0)


# Regular expressions for parsing
MSGID_RE = re.compile("^Message\-Id\: (.+)", re.IGNORECASE + re.MULTILINE)
BLANKS_RE = re.compile(r'\s+', re.MULTILINE)

# Constants
UUID = '19AF1258-1AAF-44EF-9D9A-731079D6FAD7' # Used to generate Message-Ids

def download_messages(server, filename, messages, config, countlocal, countremote, countnew):
  """Download messages from folder and append to mailbox"""
  
  if config['overwrite']:
    if os.path.exists(filename):
      if config_messahe_info_overwrite:
        print("   Deleting %s" % (filename))
      os.remove(filename)
    #return 0, 0
  else:
    assert('bzip2' != config['compress'])

  # Open disk file
  if config['compress'] == 'gzip':
    mbox = gzip.GzipFile(filename, 'ab', 9)
  elif config['compress'] == 'bzip2':
    mbox = bz2.BZ2File(filename, 'wb', 512*1024, 9)
  else:
    mbox = open(filename, 'ab')

  # the folder has already been selected by scanFolder()
  # nothing to do
  if not countnew:
    mbox.close()
    return 0, 0
  
#x  spinner = Spinner("   Downloading %s new messages to %s" % (countnew, filename))
  total = biggest = 0
 
  # each new message
  for msg_id in messages.keys():
    # This "From" and the terminating newline below delimit messages
    # in mbox files

    # Mutt expects the first line of each message to have a particular format :
    # From [ <return-path> ] <weekday> <month> <day> <time> [ <timezone> ] <year>
    # Sample: From - Fri Nov 22 15:01:23 2013
    buf = "From - %s\n" % time.strftime('%a %b %d %H:%M:%S %Y') 
    # If this is one of our synthesised Message-IDs, insert it before
    # the other headers
    if UUID in msg_id:
      buf = buf + "Message-Id: %s\n" % msg_id
    mbox.write(buf)

    # fetch message
    typ, data = server.fetch(messages[msg_id], "RFC822")
    assert('OK' == typ)
    text = data[0][1].strip().replace('\r','')
    mbox.write(text)
    mbox.write('\n\n')
    
    size = len(text)
    biggest = max(size, biggest)
    total += size
    
    del data
    gc.collect()
#x    spinner.spin()
  
  mbox.close()
#x  spinner.stop()
  return total, biggest

def scan_file(filename, compress, overwrite):
  """Gets IDs of messages in the specified mbox file"""
  # file will be overwritten
  if overwrite:
    return [], 0
  else:
    assert('bzip2' != compress)

  # file doesn't exist
  if not os.path.exists(filename):
    debugprint("File %s: not found" % (filename))
    return [], 0

#x  spinner = Spinner("   File %s" % (filename))

  # open the file
  if compress == 'gzip':
    mbox = gzip.GzipFile(filename,'rb')
  elif compress == 'bzip2':
    mbox = bz2.BZ2File(filename,'rb')
  else:
    mbox = file(filename,'rb')

  messages = {}

  # each message
  i = 0
  miwarnings = 0
  for message in mailbox.PortableUnixMailbox(mbox):
    header = ''
    # We assume all messages on disk have message-ids
    try:
      header =  ''.join(message.getfirstmatchingheader('message-id'))
    except KeyError:
      miwarnings = miwarnings + 1
      if config_message_id_warning:
        # No message ID was found. Warn the user and move on
        debugprint("")
        debugprint("WARNING: Message #%d in %s %s" % (i, filename, "has no Message-Id header."))
    
    header = BLANKS_RE.sub(' ', header.strip())
    try:
      msg_id = MSGID_RE.match(header).group(1)
      if msg_id not in messages.keys():
        # avoid adding dupes
        messages[msg_id] = msg_id
    except AttributeError:
      miwarnings = miwarnings + 1
      if config_message_id_warning:
        # Message-Id was found but could somehow not be parsed by regexp
        # (highly bloody unlikely)
        debugprint("")
        debugprint("WARNING: Message #%d in %s %s" % (i, filename, "has a malformed Message-Id header."))
      
#x    spinner.spin()
    i = i + 1

  # done
  mbox.close()
#x  spinner.stop()
  #print ": %d messages" % (len(messages.keys()))
  return messages, miwarnings

def scan_folder(server, foldername):
  """Gets IDs of messages in the specified folder, returns id:num dict"""
  messages = {}
#x  spinner = Spinner("   Folder %s" % (foldername))
  try:
    typ, data = server.select(foldername, readonly=True)
    if 'OK' != typ:
      raise SkipFolderException("SELECT failed: %s" % (data))
    num_msgs = int(data[0])
    
    # each message
    for num in range(1, num_msgs+1):
      # Retrieve Message-Id
      typ, data = server.fetch(num, '(BODY[HEADER.FIELDS (MESSAGE-ID)])')
      if 'OK' != typ:
        raise SkipFolderException("FETCH %s failed: %s" % (num, data))
      
      header = data[0][1].strip()
      # remove newlines inside Message-Id (a dumb Exchange trait)
      header = BLANKS_RE.sub(' ', header)
      try:
        msg_id = MSGID_RE.match(header).group(1) 
        if msg_id not in messages.keys():
          # avoid adding dupes
          messages[msg_id] = num
      except (IndexError, AttributeError):
        # Some messages may have no Message-Id, so we'll synthesise one
        # (this usually happens with Sent, Drafts and .Mac news)
        typ, data = server.fetch(num, '(BODY[HEADER.FIELDS (FROM TO CC DATE SUBJECT)])')
        if 'OK' != typ:
          raise SkipFolderException("FETCH %s failed: %s" % (num, data))
        header = data[0][1].strip()
        header = header.replace('\r\n','\t')
        messages['<' + UUID + '.' + sha.sha(header).hexdigest() + '>'] = num
#x      spinner.spin()
  finally:
#x    spinner.stop()
    pass
  
  # done
  return messages

def parse_paren_list(row):
  """Parses the nested list of attributes at the start of a LIST response"""
  # eat starting paren
  assert(row[0] == '(')
  row = row[1:]

  result = []

  # NOTE: RFC3501 doesn't fully define the format of name attributes :(
  name_attrib_re = re.compile("^\s*(\\\\[a-zA-Z0-9_]+)\s*")

  # eat name attributes until ending paren
  while row[0] != ')':
    # recurse
    if row[0] == '(':
      paren_list, row = parse_paren_list(row)
      result.append(paren_list)
    # consume name attribute
    else:
      match = name_attrib_re.search(row)
      assert(match != None)
      name_attrib = row[match.start():match.end()]
      row = row[match.end():]
      #print "MATCHED '%s' '%s'" % (name_attrib, row)
      name_attrib = name_attrib.strip()
      result.append(name_attrib)

  # eat ending paren
  assert(')' == row[0])
  row = row[1:]
  
  # done!
  return result, row

def parse_string_list(row):
  """Parses the quoted and unquoted strings at the end of a LIST response"""
  slist = re.compile('\s*(?:"([^"]+)")\s*|\s*(\S+)\s*').split(row)
  return [s for s in slist if s]

def parse_list(row):
  """Prases response of LIST command into a list"""
  row = row.strip()
  paren_list, row = parse_paren_list(row)
  string_list = parse_string_list(row)
  assert(len(string_list) == 2)
  return [paren_list] + string_list

def get_hierarchy_delimiter(server):
  """Queries the imapd for the hierarchy delimiter, eg. '.' in INBOX.Sent"""
  # see RFC 3501 page 39 paragraph 4
  typ, data = server.list('', '')
  assert(typ == 'OK')
  assert(len(data) == 1)
  lst = parse_list(data[0]) # [attribs, hierarchy delimiter, root name]
  hierarchy_delim = lst[1]
  # NIL if there is no hierarchy
  if 'NIL' == hierarchy_delim:
    hierarchy_delim = '.'
  return hierarchy_delim

def get_names(server, compress):
  """Get list of folders, returns [(FolderName,FileName)]"""

#x  spinner = Spinner("   Finding Folders")
  
  # Get hierarchy delimiter
  delim = get_hierarchy_delimiter(server)
#x  spinner.spin()
  
  # Get LIST of all folders
  typ, data = server.list()
  assert(typ == 'OK')
#x  spinner.spin()
  
  names = []

  # parse each LIST, find folder name
  for row in data:
    lst = parse_list(row)
    foldername = lst[2]
    suffix = {'none':'', 'gzip':'.gz', 'bzip2':'.bz2'}[compress]
    filename = '.'.join(foldername.split(delim)) + '.mbox' + suffix
    if foldername.find("[Gmail]/") != -1:
       print "Ignore([Gmail]): '%s' '%s'" % (foldername, filename)
       continue
    names.append((foldername, filename))

  # done
#x  spinner.stop()
  #print ": %s folders" % (len(names))
  return names

def process_cline():
  """Uses getopt to process command line, returns (config, warnings, errors)"""
  # read command line
  try:
    short_args = "aynzbek:c:s:u:p:"
    long_args = ["append-to-mboxes", "yes-overwrite-mboxes", "compress=",
                 "ssl", "keyfile=", "certfile=", "server=", "user=", "pass="]
    opts, extraargs = getopt.getopt(sys.argv[1:], short_args, long_args)
  except getopt.GetoptError:
    print_usage()
  
  warnings = []
  config = {'compress':'none', 'overwrite':False, 'usessl':False}
  errors = []

  # empty command line
  if not len(opts) and not len(extraargs):
    print_usage()
  
  # process each command line option, save in config
  for option, value in opts:
    if option in ("-a", "--append-to-mboxes"):
      config['overwrite'] = False
    elif option in ("-y", "--yes-overwrite-mboxes"):
      warnings.append("Existing mbox files will be overwritten!")
      config["overwrite"] = True
    elif option == "-n":
      config['compress'] = 'none'
    elif option == "-z":
      config['compress'] = 'gzip'
    elif option == "-b":
      config['compress'] = 'bzip2'
    elif option == "--compress":
      if value in ('none', 'gzip', 'bzip2'):
        config['compress'] = value
      else:
        errors.append("Invalid compression type specified.")
    elif option in ("-e", "--ssl"):
      config['usessl'] = True
    elif option in ("-k", "--keyfile"):
      config['keyfilename'] = value
    elif option in ("-c", "--certfile"):
      config['certfilename'] = value
    elif option in ("-s", "--server"):
      config['server'] = value
    elif option in ("-u", "--user"):
      config['user'] = value
    elif option in ("-p", "--pass"):
      config['pass'] = value
    else:
      errors.append("Unknown option: " + option)

  # don't ignore extra arguments
  for arg in extraargs:
    errors.append("Unknown argument: " + arg)
  
  # done processing command line
  return (config, warnings, errors)

def check_config(config, warnings, errors):
  """Checks the config for consistency, returns (config, warnings, errors)"""

  if config['compress'] == 'bzip2' and config['overwrite'] == False:
    errors.append("Cannot append new messages to mbox.bz2 files.  Please specify -y.")
  if config['compress'] == 'gzip' and config['overwrite'] == False:
    warnings.append(
      "Appending new messages to mbox.gz files is very slow.  Please Consider\n"
      "  using -y and compressing the files yourself with gzip -9 *.mbox")
  if 'server' not in config :
    errors.append("No server specified.")
  if 'user' not in config:
    errors.append("No username specified.")
  if ('keyfilename' in config) ^ ('certfilename' in config):
    errors.append("Please specify both key and cert or neither.")
  if 'keyfilename' in config and not config['usessl']:
    errors.append("Key specified without SSL.  Please use -e or --ssl.")
  if 'certfilename' in config and not config['usessl']:
    errors.append("Certificate specified without SSL.  Please use -e or --ssl.")
  if 'server' in config and ':' in config['server']:
    # get host and port strings
    bits = config['server'].split(':', 1)
    config['server'] = bits[0]
    # port specified, convert it to int
    if len(bits) > 1 and len(bits[1]) > 0:
      try:
        port = int(bits[1])
        if port > 65535 or port < 0:
          raise ValueError
        config['port'] = port
      except ValueError:
        errors.append("Invalid port.  Port must be an integer between 0 and 65535.")
  return (config, warnings, errors)
  
def get_config():
  """Gets config from command line and console, returns config"""
  # config = {
  #   'compress': 'none' or 'gzip' or 'bzip2'
  #   'overwrite': True or False
  #   'server': String
  #   'port': Integer
  #   'user': String
  #   'pass': String
  #   'usessl': True or False
  #   'keyfilename': String or None
  #   'certfilename': String or None
  # }
  
  config, warnings, errors = process_cline()
  config, warnings, errors = check_config(config, warnings, errors)
  
  # show warnings
  for warning in warnings:
    print "WARNING:", warning
  
  # show errors, exit
  for error in errors:
    print "ERROR", error
  if len(errors):
    sys.exit(2)

  # prompt for password, if necessary
  if 'pass' not in config:
    config['pass'] = getpass.getpass()
  
  # defaults
  if not 'port' in config:
    if config['usessl']:
      config['port'] = 993
    else:
      config['port'] = 143
  
  # done!
  return config

def connect_and_login(config):
  """Connects to the server and logs in.  Returns IMAP4 object."""
  try:
    assert(not (('keyfilename' in config) ^ ('certfilename' in config)))
    
    if config['usessl'] and 'keyfilename' in config:
      print "Connecting to '%s' TCP port %d," % (config['server'], config['port']),
      print "SSL, key from %s," % (config['keyfilename']),
      print "cert from %s " % (config['certfilename'])
      server = imaplib.IMAP4_SSL(config['server'], config['port'],
                                 config['keyfilename'], config['certfilename'])
    elif config['usessl']:
      print "Connecting to '%s' TCP port %d, SSL" % (config['server'], config['port'])
      server = imaplib.IMAP4_SSL(config['server'], config['port'])
    else:
      print "Connecting to '%s' TCP port %d" % (config['server'], config['port'])
      server = imaplib.IMAP4(config['server'], config['port'])
    
    tweaksocket(server)
    print "Logging in as '%s'" % (config['user'])
    server.login(config['user'], config['pass'])
  except socket.gaierror, e:
    (err, desc) = e
    print "ERROR: problem looking up server '%s' (%s %s)" % (config['server'], err, desc)
    sys.exit(3)
  except socket.error, e:
    if str(e) == "SSL_CTX_use_PrivateKey_file error":
      print "ERROR: error reading private key file '%s'" % (config['keyfilename'])
    elif str(e) == "SSL_CTX_use_certificate_chain_file error":
      print "ERROR: error reading certificate chain file '%s'" % (config['keyfilename'])
    else:
      print "ERROR: could not connect to '%s' (%s)" % (config['server'], e)
    
    sys.exit(4)

  return server

def submain(server, foldername, filename, config):
        fol_messages = scan_folder(server, foldername) ;# remote scan
        fil_messages, miwarnings = scan_file(filename, config['compress'], config['overwrite']) ;# local scan

        countremote = len(fol_messages) # remote total emails
        countlocal = len(fil_messages)  # already got (localy) emails
        countnew = countremote - countlocal
        new_messages = {}
        for msg_id in fol_messages:
          if msg_id not in fil_messages:
            new_messages[msg_id] = fol_messages[msg_id]
        
        #for f in new_messages:
        #  print "%s : %s" % (f, new_messages[f])

        sizetotal, sizebiggest = download_messages(server, filename, new_messages, config, countlocal, countremote, countnew)
        sizenew = 0

	if(countnew == 0 and sizetotal == 0):
          sizetotal = sizenew = "-"
        else:
          sizenew     = pretty_byte_count(sizenew)
          sizetotal   = pretty_byte_count(sizetotal)

	miwarntxt = ""
        if(miwarnings > 0):
          miwarntxt = " (%d warnings)" % miwarnings

        part1 = "[%5d new] [local %5d/%5d remote] [%s/%s] %s" % (countnew, countlocal, countremote, sizenew, sizetotal, filename)
        if(sizebiggest > 0):
          part2 = " (%s for largest message)%s" % ( pretty_byte_count(sizebiggest), miwarntxt)
        else:
          part2 = "%s" % (miwarntxt)
        print part1+part2



def main():
  """Main entry point"""
  try:
    config = get_config()
    server = connect_and_login(config)
    names = get_names(server, config['compress'])
    names.reverse()

    #for n in range(len(names)):
    #  print n, names[n]

    for name_pair in names:
      foldername, filename = name_pair
      
      try:
        submain(server, foldername, filename, config)
      except SkipFolderException, e:
        print e
    
    #print "Disconnecting"
    server.logout()
  except socket.error, e:
    (err, desc) = e
    print "ERROR: %s %s" % (err, desc)
    sys.exit(4)
  except imaplib.IMAP4.error, e:
    print "ERROR:", e
    sys.exit(5)


# From http://www.pixelbeat.org/talks/python/spinner.py
def cli_exception(typ, value, traceback):
  """Handle CTRL-C by printing newline instead of ugly stack trace"""
  if not issubclass(typ, KeyboardInterrupt):
    sys.__excepthook__(typ, value, traceback)
  else:
    sys.stdout.write("\n")
    sys.stdout.flush()

if sys.stdin.isatty():
  sys.excepthook = cli_exception



# Hideous fix to counteract http://python.org/sf/1092502
# (which should have been fixed ages ago.)
# Also see http://python.org/sf/1441530
def _fixed_socket_read(self, size=-1):
  data = self._rbuf
  if size < 0:
    # Read until EOF
    buffers = []
    if data:
      buffers.append(data)
    self._rbuf = ""
    if self._rbufsize <= 1:
      recv_size = self.default_bufsize
    else:
      recv_size = self._rbufsize
    while True:
      data = self._sock.recv(recv_size)
      if not data:
        break
      buffers.append(data)
    return "".join(buffers)
  else:
    # Read until size bytes or EOF seen, whichever comes first
    buf_len = len(data)
    if buf_len >= size:
      self._rbuf = data[size:]
      return data[:size]
    buffers = []
    if data:
      buffers.append(data)
    self._rbuf = ""
    while True:
      left = size - buf_len
      recv_size = min(self._rbufsize, left) # the actual fix
      data = self._sock.recv(recv_size)
      if not data:
        break
      buffers.append(data)
      n = len(data)
      if n >= left:
        self._rbuf = data[left:]
        buffers[-1] = data[:left]
        break
      buf_len += n
    return "".join(buffers)

# Platform detection to enable socket patch
if 'Darwin' in platform.platform() and '2.3.5' == platform.python_version():
  socket._fileobject.read = _fixed_socket_read
if 'Windows' in platform.platform():
  socket._fileobject.read = _fixed_socket_read

if __name__ == '__main__':
  gc.enable()
  main()
