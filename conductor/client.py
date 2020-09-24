import asyncio, logging, argparse, sys, json, os, signal, secrets, getpass, subprocess
from functools import partial
from tempfile import NamedTemporaryFile
from enum import unique, IntEnum
from hashlib import blake2b

import asyncssh, aionotify

from .util import proxy, socketListener

logger = logging.getLogger (__name__)

def randomSecret (n):
	alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
	return ''.join (secrets.choice (alphabet) for i in range (n))

def writeJson (d):
	""" Write a single json object to stdout and terminate with newline """
	json.dump (d, sys.stdout)
	sys.stdout.write ('\n')
	sys.stdout.flush ()

@unique
class ExitCode (IntEnum):
	OK = 0
	ERROR = 1
	SOCKET_USED = 2

class Client ():
	"""
	conductor client

	Connects to remote SSH server and manages incoming connections from remote
	socket. Writes config file to pipe client.
	"""

	def __init__ (self, forestpath, pipeCmd, localsocket, user, host, port, command,
			token, replace=False, key=None):
		self.user = user
		self.host = host
		self.port = port
		self.forestpath = os.path.realpath (forestpath)
		self.pipeCmd = pipeCmd
		self.localsocket = localsocket
		self.command = command
		self.token = token
		self.replace = replace
		self.key = key
		self.nextConnId = 0

	def connectLocal (self):
		return asyncio.open_unix_connection (path=self.localsocket)

	async def handler (self, reader, writer):
		connid = self.nextConnId
		self.nextConnId += 1
		logger.debug (f'{connid}: new connection')

		try:
			sockreader, sockwriter = await self.connectLocal ()
		except Exception as e:
			logger.error (f'{connid}: local socket {self.localsocket} not avaiable, {e}')
			writer.close ()
			return

		await proxy ((sockreader, sockwriter, 'sock'), (reader, writer, 'ssh'), logger=logger, logPrefix=connid)

	def accept (self):
		return self.handler

	@staticmethod
	def checkSocketExists (path, replace=False):
		if os.path.exists (path):
			try:
				for pid in socketListener (path):
					if replace:
						logger.error (f'Killing PID {pid}')
						os.kill (pid, signal.SIGTERM)
					else:
						logger.error (f'PID {pid} is already listening on {path}')
						raise FileExistsError ()
			except KeyError:
				logger.debug (f'removing stale socket {path}')
				os.unlink (path)

	async def run (self):
		async def wrapFd (fd, kind):
			""" Wrap data from fd into json messages """
			while True:
				buf = await fd.read (4*1024)
				if not buf:
					break
				writeJson (dict (
						state='data',
						kind=kind,
						data=buf.decode ('utf-8', errors='replace'),
						))

		try:
			self.checkSocketExists (self.localsocket, self.replace)
		except FileExistsError:
			return ExitCode.SOCKET_USED

		writeJson (dict (state='connect', user=self.user, host=self.host, port=self.port))
		# remove null arguments
		connectArgs = dict (filter (lambda x: x[1], [('host', self.host), ('port', self.port), ('user', self.user)]))
		async with asyncssh.connect (**connectArgs) as conn:
			commandproc = await asyncio.create_subprocess_exec (self.command[0],
					*self.command[1:], start_new_session=True,
					stdout=subprocess.PIPE, stderr=subprocess.PIPE)
			stdoutTask = asyncio.create_task (wrapFd (commandproc.stdout, 'stdout'))
			stderrTask = asyncio.create_task (wrapFd (commandproc.stderr, 'stderr'))
			writeJson (dict (state='execute', command=self.command, pid=commandproc.pid))

			pipeproc = await conn.create_process (f'{self.pipeCmd} {self.forestpath}')
			writeJson (dict (state='pipe', pipeCmd=self.pipeCmd, forestPath=self.forestpath))

			tries = 0
			while True:
				sockName = getpass.getuser() + '-' + randomSecret (16) + '.socket'
				sockpath = os.path.join (self.forestpath, sockName)
				writeJson (dict (state='socket', socketPath=sockpath))
				try:
					listener = await conn.start_unix_server (self.accept,
							listen_path=sockpath)
					break
				except asyncssh.misc.ChannelListenError:
					# generate a different name
					logger.debug ('failed starting unix server, try {tries}')
					tries += 1
					if tries > 5:
						logger.error (f'cannot create a socket on remote forest {self.forestpath}, check permissions')
						return ExitCode.ERROR

			config = {'socket': sockName, 'auth': self.token}
			if self.key:
				config['key'] = self.key
			writeJson (dict (state='config', config=config))
			config['auth'] = blake2b (self.token.encode ('utf-8')).hexdigest ()
			pipeproc.stdin.write (json.dumps (config) + '\n')

			# wait for the application to become live, i.e. socket exists and server responds
			while True:
				try:
					await self.connectLocal ()
					logger.debug (f'local socket {self.localsocket} is available, moving on')
					break
				except Exception as e:
					logger.debug (f'local socket {self.localsocket} not avaiable yet ({e}), waiting')
					return ExitCode.ERROR
					await asyncio.sleep (0.5)

			try:
				buf = await pipeproc.stdout.readline ()
				config = json.loads (buf)
				# check server response
				status = config.get ('status', 'ok')
				if status == 'error':
					writeJson (dict (state='failed', reason=config.get ('reason', 'No reason given')))
					return ExitCode.ERROR
				# Server only knows the hashed token, but client should get the unhashed value.
				config['auth'] = self.token

				writeJson (dict (state='live', config=config))
				done, pending = await asyncio.wait ([pipeproc.wait (), listener.wait_closed (),
						commandproc.wait ()], return_when=asyncio.FIRST_COMPLETED)
			except asyncio.CancelledError:
				logger.debug (f'cancelled')
			finally:
				ret = commandproc.returncode
				if ret is None:
					logger.debug ('terminating command')
					# We’re starting commandproc above with
					# start_new_session=True, which means it will be leader of
					# a new process group with its PID. Kill the whole process
					# group in case the started process does not forward
					# signals to its children.
					os.killpg (commandproc.pid, signal.SIGTERM)
					try:
						ret = await asyncio.wait_for(commandproc.wait (), timeout=3.0)
					except asyncio.TimeoutError:
						os.killpg (commandproc.pid, signal.SIGKILL)
						ret = await commandproc.wait ()
				writeJson (dict (state='exit', status=ret))

				# cleanup
				pipeproc.terminate ()
				ret = await pipeproc.wait ()
				if ret.exit_status != 0:
					writeJson (dict (
							state='failed',
							reason='pipe',
							status=ret.exit_status,
							stderr=await pipeproc.stderr.read (),
							stdout=await pipeproc.stdout.read ()),
							)
					return ExitCode.ERROR

				for task in (stdoutTask, stderrTask):
					task.cancel ()
					try:
						await task
					except asyncio.CancelledError:
						pass
		return ExitCode.OK

def parseSSHPath (s):
	if '@' in s:
		user, tail = s.split ('@', 1)
	else:
		user, tail = None, s
	host, path = tail.split (':', 1)
	return user, host, path

def main ():
	parser = argparse.ArgumentParser(description='conductor client')
	parser.add_argument ('-c', '--pipe', default='conductor-pipe', help='Remote pipe command')
	parser.add_argument ('-p', '--port', type=int, default=22, help='SSH port')
	parser.add_argument ('-r', '--replace', action='store_true', help='Replace existing process')
	parser.add_argument ('-v', '--verbose', action='store_true', help='Verbose output')
	parser.add_argument ('-k', '--key', default='', help='Subdomain')
	parser.add_argument ('forest', type=parseSSHPath,
			help='Remote forest path, i.e. user@host:/path')
	parser.add_argument ('socket', help='Local socket to connect to')
	parser.add_argument ('command', nargs=argparse.REMAINDER, help='Command to run')

	args = parser.parse_args()
	if args.verbose:
		logging.basicConfig (level=logging.DEBUG)
	else:
		logging.basicConfig (level=logging.WARN)

	token = os.getenv ('CONDUCTOR_TOKEN', None)
	if token is not None:
		# make sure the token does not spill into any subprocesses we start
		os.unsetenv ('CONDUCTOR_TOKEN')
	else:
		# generate a random one, if none supplied
		token = randomSecret (32)

	client = Client (args.forest[2], args.pipe, args.socket, args.forest[0],
			args.forest[1], args.port,
			args.command, token, replace=args.replace, key=args.key)

	run = asyncio.ensure_future (client.run ())
	loop = asyncio.get_event_loop ()
	stop = lambda signum: run.cancel ()
	for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
		loop.add_signal_handler (sig, stop, sig)
	try:
		return loop.run_until_complete (run)
	except asyncio.CancelledError:
		pass

async def pipeAsync (readfd, writefd, forest):
	# make fd asyncio aware
	reader = asyncio.StreamReader ()
	loop = asyncio.get_event_loop ()
	await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), readfd)

	# files removed when exiting
	cleanupFiles = []

	try:
		l = await reader.readline ()

		config = json.loads (l)

		fd = NamedTemporaryFile (mode='w',
				prefix=os.path.basename (config['socket']) + '-',
				dir=forest,
				delete=False)
		json.dump (config, fd)
		fd.close ()

		# fix permissions on config and socket
		os.chmod (fd.name, 0o660)
		if 'socket' in config:
			socketPath = os.path.join (forest, config['socket'])
			if os.path.exists (socketPath):
				os.chmod (socketPath, 0o660)
				cleanupFiles.append (socketPath)

		# rename to make server pick up the file
		os.rename (fd.name, fd.name + '.json')
		configPath = fd.name + '.json'
		cleanupFiles.append (configPath)

		# watch the file for server changes and write them to stdout
		watcher = aionotify.Watcher()
		watcher.watch (path=configPath, flags=aionotify.Flags.CLOSE_WRITE)

		await watcher.setup (loop)
		while True:
			event = await watcher.get_event()
			if event.flags == aionotify.Flags.CLOSE_WRITE:
				with open (configPath, 'r') as fd:
					writefd.write (fd.read ())
					# pipes are not line-buffered, explicit flush required
					writefd.flush ()
			else:
				assert False
		watcher.close ()
	except asyncio.CancelledError:
		pass
	finally:
		for f in cleanupFiles:
			try:
				os.unlink (f)
			except FileNotFoundError:
				pass

def pipe ():
	parser = argparse.ArgumentParser(description='conductor pipe (do not use directly)')
	parser.add_argument ('forest', help='Local forest path')

	args = parser.parse_args()

	run = asyncio.ensure_future (pipeAsync (sys.stdin, sys.stdout, args.forest))
	loop = asyncio.get_event_loop ()

	# connect signals
	stop = lambda signum: run.cancel ()
	for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGPIPE):
		loop.add_signal_handler (sig, stop, sig)

	try:
		loop.run_until_complete (run)
	except asyncio.CancelledError:
		pass

