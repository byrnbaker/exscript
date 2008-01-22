#!/usr/bin/env python
## Author:      Samuel Abels
## Date:        2007-06-04
## Description: Use the Exscript interpreter with a multi threaded configuration
##              engine to execute commands on a list of hosts.
import sys, time, os, re, signal, gc, copy, socket, getpass
sys.path.insert(0, 'lib')
import Exscript
from FooLib          import Interact
from FooLib          import OptionParser
from FooLib          import UrlParser
from WorkQueue       import WorkQueue
from WorkQueue       import Sequence
from TerminalActions import *

True  = 1
False = 0

__version__ = '0.9.11'

def usage():
    print "Exscript %s" % __version__
    print "Copyright (C) 2007 by Samuel Abels <http://debain.org>."
    print "Syntax: ./exscript.py [options] exscript [hostname [hostname ...]]"
    print "  -a, --authorize"
    print "                 When given, authorization is performed on devices that"
    print "                 support AAA (by default, Exscript only authenticates)"
    print "  -A, --authorize2"
    print "                 Like -a, but uses the authentication password instead of"
    print "                 asking for a password to be entered."
    print "  -c, --connections=NUM"
    print "                 Maximum number of concurrent connections."
    print "                 NUM is a number between 1 and 20, default is 1"
    print "      --csv-hosts FILE"
    print "                 Loads a list of hostnames and definitions from the given file."
    print "                 The first line of the file must have the column headers in the"
    print "                 following syntax:"
    print "                    hostname [variable] [variable] ..."
    print "                 where the fields are separated by tabs, \"hostname\" is the"
    print "                 keyword \"hostname\" and \"variable\" is a unique name under"
    print "                 which the column is accessed in the script."
    print "                 The following lines contain the hostname in the first column,"
    print "                 and the values of the variables in the following columns."
    print "  -d, --define PAIR"
    print "                 Defines a variable that is passed to the script."
    print "                 PAIR has the following syntax: <STRING>=<STRING>."
    print "      --hosts FILE"
    print "                 Loads a list of hostnames from the given file (one host per"
    print "                 line)."
    print "  -l, --logdir DIR"
    print "                 Logs any communication into the directory with the given name."
    print "                 Each filename consists of the hostname with \"_log\" appended."
    print "                 Errors are written to a separate file, where the filename"
    print "                 consists of the hostname with \".log.error\" appended."
    print "      --no-echo"
    print "                 Turns off the echo, such that the network activity is no longer"
    print "                 written to stdout."
    print "                 This is already the default behavior if the -c option was given"
    print "                 with a number greater than 1."
    print "  -n, --no-authentication"
    print "                 When given, the authentication procedure is skipped."
    print "      --no-auto-logout"
    print "                 Do not attempt to execute the exit or quit command at the end"
    print "                 of a script."
    print "      --no-prompt"
    print "                 Do not wait for a prompt anywhere. Note that this will also"
    print "                 cause Exscript to disable commands that require a prompt, such"
    print "                 as 'extract'."
    print "      --no-initial-prompt"
    print "                 Do not wait for a prompt after sending the password."
    print "  -p, --protocol STRING"
    print "                 Specify which protocol to use to connect to the remote host."
    print "                 STRING is one of: telnet ssh"
    print "                 The default protocol is telnet."
    print "  -v, --verbose NUM"
    print "                 Print out debug information about the network activity."
    print "                 NUM is a number between 0 (min) and 5 (max)"
    print "  -V, --parser-verbose NUM"
    print "                 Print out debug information about the Exscript parser."
    print "                 NUM is a number between 0 (min) and 5 (max)"
    print "      --version  Prints the version number."
    print "  -h, --help     Prints this help."

# Define default options.
default_defines = {'hostname': 'unknown'}
default_options = [
  ('authorize',         'a',  False),
  ('authorize2',        'A',  False),
  ('no-echo',           None, False),
  ('connections=',      'c:', 1),
  ('csv-hosts=',        None, None),
  ('define=',           'd:', default_defines),
  ('hosts=',            None, None),
  ('logdir=',           'l:', None),
  ('protocol=',         'p:', 'telnet'),
  ('no-authentication', 'n',  False),
  ('no-prompt',         None, False),
  ('no-initial-prompt', None, False),
  ('no-auto-logout',    None, False),
  ('verbose=',          'v:', 0),
  ('parser-verbose=',   'V:', 0),
  ('version',           None, False),
  ('help',              'h',  False)
]

def exscript(*args, **kwargs):
    options = {}
    for option, short_option, value in default_options:
        option = re.sub(r'=$', '', option)
        options[option] = value
    options.update(kwargs)
    options['define'].update(default_defines)

    # Show the help, if requested.
    if options['help']:
        usage()
        sys.exit()

    # Show the version number, if requested.
    if options['version']:
        print "Exscript %s" % __version__
        sys.exit()

    # Check command line syntax.
    if options['authorize'] and options['authorize2']:
        print "Error: Can't use both, -a and -A switch."
        sys.exit(1)
    if options['no-authentication'] and options['authorize']:
        print "Error: Can't use both, -n and -a switch."
        sys.exit(1)
    if options['no-authentication'] and options['authorize2']:
        print "Error: Can't use both, -n and -A switch."
        sys.exit(1)

    try:
        exscript  = args[0]
        hostnames = list(args[1:])
    except:
        usage()
        sys.exit(1)
    defines = dict([(hostname, options['define']) for hostname in hostnames])

    # If a filename containing hostnames AND VARIABLES was given, read it.
    if options.get('csv-hosts') is not None:
        # Make sure that the file exists.
        if not os.path.exists(options.get('csv-hosts')):
            print "Error: File '%s' not found." % options.get('csv-hosts')
            sys.exit(1)

        # Open the file.
        try:
            file = open(options.get('csv-hosts'), 'r')
        except:
            print "Unable to open file '%s'. Perhaps you do not have read permission?"
            sys.exit(1)

        # Read the header.
        header = file.readline().rstrip()
        if re.search(r'^hostname\b', header) is None:
            print "Syntax error in CSV file header: File does not start with \"hostname\"."
            sys.exit(1)
        if re.search(r'^hostname(?:\t[^\t]+)*$', header) is None:
            print "Syntax error in CSV file header: %s" % header
            print "Make sure to separate columns by tabs."
            sys.exit(1)
        varnames = header.split('\t')
        varnames.pop(0)
        
        # Walk through all lines and create a map that maps hostname to definitions.
        last_hostname = ''
        for line in file:
            line     = re.sub(r'[\r\n]*$', '', line)
            values   = line.split('\t')
            hostname = values.pop(0).strip()

            # Add the hostname to our list.
            if hostname != last_hostname:
                #print "Reading hostname", hostname, "from csv."
                hostnames.append(hostname)
                defines[hostname] = options['define'].copy()
                last_hostname = hostname

            # Define variables according to the definition.
            for i in range(0, len(varnames)):
                varname = varnames[i]
                try:
                    value = values[i]
                except:
                    value = ''
                if defines[hostname].has_key(varname):
                    defines[hostname][varname].append(value)
                else:
                    defines[hostname][varname] = [value]

    # If a filename containing hostnames was given, read it.
    if options.get('hosts') is not None:
        # Make sure that the file exists.
        if not os.path.exists(options.get('hosts')):
            print "Error: File '%s' not found." % options.get('hosts')
            sys.exit(1)

        # Open the file.
        try:
            file = open(options.get('hosts'), 'r')
        except:
            print "Unable to open file '%s'. Perhaps you do not have read permission?"
            sys.exit(1)

        # Read the hostnames.
        for line in file:
            hostname = line.strip()
            if hostname == '':
                continue
            hostnames.append(hostname)
            defines[hostname] = options['define'].copy()

    # Create the log directory.
    if options.get('logdir') is not None:
        if not os.path.exists(options.get('logdir')):
            print 'Creating log directory (%s)...' % options.get('logdir')
            try:
                os.makedirs(options.get('logdir'))
            except:
                print 'Error: Unable to create directory %s.' % options.get('logdir')
                sys.exit(1)

    # Make sure that all mandatory options are present.
    if len(hostnames) <= 0:
        usage()
        sys.exit(1)

    # Read the Exscript.
    try:
        file = open(exscript, 'r')
    except:
        print "Unable to open '%s'. Perhaps you do not have read permission?" % exscript
        sys.exit(1)
    exscript_content = file.read()
    file.close()

    # Prepare the code that is executed after the user script has completed.
    if not options['no-auto-logout']:
        exscript_content += r'''
    ## Exscript generated commands. ##
    {if device.os(0) is "vrp"}
        {connection.send("quit\r", 0)}
    {else}
        {connection.send("exit\r", 0)}
    {end}'''

    # Initialize the parser.
    parser = Exscript.Parser(debug     = options['parser-verbose'],
                             no_prompt = options['no-prompt'])
    parser.define(**defines[hostnames[0]])
    _, _, _, _, this_query = UrlParser.parse_url(hostnames[0])
    parser.define(**this_query)

    # Parse the exscript.
    try:
        excode = parser.parse(exscript_content)
    except Exception, e:
        if options['verbose'] > 0:
            raise
        print e
        sys.exit(1)

    # Read username and password.
    try:
        if options['no-authentication']:
            user     = None
            password = None
        else:
            user, password = Interact.get_login()
        if options['authorize']:
            msg       = 'Please enter your authorization password: '
            password2 = getpass.getpass(msg)
    except:
        sys.exit(1)

    # Make sure that we shut down properly even when SIGINT or SIGTERM is sent.
    def on_posix_signal(signum, frame):
        print '******************* SIGINT RECEIVED - SHUTTING DOWN! *******************'
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT,  on_posix_signal)
    signal.signal(signal.SIGTERM, on_posix_signal)

    try:
        # Initialize the workqueue.
        workqueue = WorkQueue(max_threads = options['connections'],
                              debug       = options['verbose'])

        print 'Starting engine...'
        workqueue.start()
        print 'Engine running.'

        # Build the action sequence.
        print 'Building sequence...'
        for hostname in hostnames:
            # To save memory, limit the number of parsed (=in-memory) items.
            while workqueue.get_queue_length() > options['connections'] * 2:
                time.sleep(1)
                gc.collect()

            if options['verbose'] > 0:
                print 'Building sequence for %s.' % hostname

            # Prepare variables that are passed to the exscript interpreter.
            (this_proto,
             this_user,
             this_pass,
             this_host,
             this_query) = UrlParser.parse_url(hostname, options['protocol'])
            variables             = defines[hostname]
            variables['hostname'] = this_host
            variables.update(this_query)
            if this_user is None:
                this_user = user
                this_pass = password

            #FIXME: In Python > 2.2 we can (hopefully) deep copy the object instead of
            # recompiling numerous times.
            excode = parser.parse(exscript_content)
            #excode = copy.deepcopy(excode)
            excode.init(**variables)

            # One logfile per host.
            logfile       = None
            error_logfile = None
            if options.get('logdir') is None:
                sequence = Sequence(name = this_host)
            else:
                logfile       = os.path.join(options.get('logdir'), this_host + '.log')
                error_logfile = logfile + '.error'
                sequence      = LoggedSequence(name          = this_host,
                                               logfile       = logfile,
                                               error_logfile = error_logfile)

            # Choose the protocol.
            if this_proto == 'telnet':
                protocol = __import__('TerminalConnection.Telnet',
                                      globals(),
                                      locals(),
                                      'Telnet')
            elif this_proto == 'ssh':
                protocol = __import__('TerminalConnection.SSH',
                                      globals(),
                                      locals(),
                                      'SSH')
            else:
                print 'Unsupported protocol %s' % this_proto
                continue

            # Build the sequence.
            echo = options['connections'] == 1 and options['no-echo'] == 0
            wait = not options['no-initial-prompt'] and not options['no-prompt']
            sequence.add(Connect(protocol, this_host, echo = echo))
            if not options['no-authentication']:
                sequence.add(Authenticate(this_user, this_pass, wait))
            if options['authorize']:
                sequence.add(Authorize(password2, wait))
            if options['authorize2']:
                sequence.add(Authorize(this_pass, wait))
            sequence.add(CommandScript(excode))
            sequence.add(Close())
            workqueue.enqueue(sequence)

        # Wait until the engine is finished.
        print 'All actions enqueued.'
        while workqueue.get_queue_length() > 0:
            #print '%s jobs left, waiting.' % workqueue.get_queue_length()
            time.sleep(1)
            gc.collect()
        print 'Shutting down engine...'
    except KeyboardInterrupt:
        print 'Interrupt caught succcessfully.'
        print '%s unfinished jobs.' % workqueue.get_queue_length()
        sys.exit(1)

    workqueue.shutdown()
    print 'Engine shut down.'

if __name__ == '__main__':
    # Parse options.
    try:
        options, args = OptionParser.parse_options(sys.argv, default_options)
    except:
        usage()
        sys.exit(1)

    exscript(*args, **options)
