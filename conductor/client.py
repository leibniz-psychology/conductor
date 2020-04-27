import asyncio, logging, argparse, sys, json, os, signal, secrets, getpass
from functools import partial
from tempfile import NamedTemporaryFile
from enum import unique, IntEnum
import asyncssh

from .util import proxy, socketListener

logger = logging.getLogger (__name__)

def randomSecret (n):
	alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
	return ''.join (secrets.choice (alphabet) for i in range (n))

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

	def __init__ (self, forestpath, pipeCmd, localsocket, host, port, command,
			token, replace=False, key=None):
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

	async def handler (self, reader, writer):
		connid = self.nextConnId
		self.nextConnId += 1
		logger.debug (f'{connid}: new connection')

		try:
			sockreader, sockwriter = await asyncio.open_unix_connection (path=self.localsocket)
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
		try:
			self.checkSocketExists (self.localsocket, self.replace)
		except FileExistsError:
			return ExitCode.SOCKET_USED

		logger.debug (f'connecting to {self.host}:{self.port} via SSH')
		async with asyncssh.connect (self.host, port=self.port) as conn:
			logger.debug (f'executing command {self.command}')
			commandproc = await asyncio.create_subprocess_exec (self.command[0],
					*self.command[1:], start_new_session=True)

			logger.debug (f'running pipe {self.pipeCmd} {self.forestpath}')
			pipeproc = await conn.create_process (f'{self.pipeCmd} {self.forestpath}')

			tries = 0
			while True:
				sockName = getpass.getuser() + '-' + randomSecret (16) + '.socket'
				sockpath = os.path.join (self.forestpath, sockName)
				try:
					logger.debug (f'starting unix server on {sockpath}')
					listener = await conn.start_unix_server (self.accept,
							listen_path=sockpath)
					break
				except asyncssh.misc.ChannelListenError:
					# generate a different name
					logger.debug ('failed starting unix server, try {tries}')
					tries += 1
					if tries > 5:
						logger.error (f'cannot create a socket on remote forest {self.forestpath}, check permissions')
						return

			config = {'socket': sockpath, 'auth': self.token}
			if self.key:
				config['key'] = self.key
			logger.debug (f'writing config to pipe {config}')
			pipeproc.stdin.write (json.dumps (config) + '\n')

			try:
				await asyncio.wait ([pipeproc.wait (), listener.wait_closed (),
						commandproc.wait ()], return_when=asyncio.FIRST_COMPLETED)
			except asyncio.CancelledError:
				logger.debug (f'cancelled')
			finally:
				if commandproc.returncode is None:
					logger.debug ('terminating command')
					commandproc.terminate ()
					try:
						await asyncio.wait_for(commandproc.wait (), timeout=3.0)
					except asyncio.TimeoutError:
						commandproc.kill ()
					ret = await commandproc.wait ()
					logger.debug (f'command returned {ret}')
		return ExitCode.OK

def parseSSHPath (s):
	host, path = s.split (':', 1)
	return host, path

def main ():
	parser = argparse.ArgumentParser(description='conductor client')
	parser.add_argument ('-c', '--pipe', default='conductor-pipe', help='Remote pipe command')
	parser.add_argument ('-p', '--port', type=int, default=22, help='SSH port')
	parser.add_argument ('-r', '--replace', action='store_true', help='Replace existing process')
	parser.add_argument ('-v', '--verbose', action='store_true', help='Verbose output')
	parser.add_argument ('-k', '--key', default='', help='Subdomain')
	parser.add_argument ('forest', type=parseSSHPath, help='Remote forest path')
	parser.add_argument ('socket', help='Local socket to connect to')
	parser.add_argument ('command', nargs=argparse.REMAINDER, help='Command to run')

	args = parser.parse_args()
	if args.verbose:
		logging.basicConfig (level=logging.DEBUG)
	else:
		logging.basicConfig (level=logging.WARN)

	token = os.getenv ('CONDUCTOR_TOKEN', None)
	if token is None:
		parser.exit (1, 'Missing environment variable CONDUCTOR_TOKEN\n')
	# make sure the token does not spill into any subprocesses we start
	os.unsetenv ('CONDUCTOR_TOKEN')

	client = Client (args.forest[1], args.pipe, args.socket, args.forest[0], args.port,
			args.command, token, replace=args.replace, key=args.key)

	run = asyncio.ensure_future (client.run ())
	loop = asyncio.get_event_loop ()
	stop = lambda signum: run.cancel ()
	for sig in (signal.SIGINT, signal.SIGTERM):
		loop.add_signal_handler (sig, stop, sig)
	try:
		return loop.run_until_complete (run)
	except asyncio.CancelledError:
		pass

async def pipeAsync (fd, forest):
	# make fd asyncio aware
	reader = asyncio.StreamReader ()
	loop = asyncio.get_event_loop ()
	await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), fd)

	fd = None
	lastConfig = None
	lastConfigPath = None

	try:
		while True:
			l = await reader.readline ()
			if not l:
				break

			lastConfig = json.loads (l)

			fd = NamedTemporaryFile (mode='w',
					prefix=os.path.basename (lastConfig['socket']) + '-',
					dir=forest,
					delete=False)
			json.dump (lastConfig, fd)
			fd.close ()

			# fix permissions on config and socket
			os.chmod (fd.name, 0o640)
			os.chmod (lastConfig['socket'], 0o660)

			# rename to make server pick up the file
			os.rename (fd.name, fd.name + '.json')
			lastConfigPath = fd.name + '.json'
	except asyncio.CancelledError:
		pass
	finally:
		if fd and os.path.exists (fd.name):
			os.unlink (fd.name)
		if lastConfig and 'socket' in lastConfig and os.path.exists (lastConfig['socket']):
			os.unlink (lastConfig['socket'])
		if lastConfigPath and os.path.exists (lastConfigPath):
			os.unlink (lastConfigPath)

def pipe ():
	parser = argparse.ArgumentParser(description='conductor pipe (do not use directly)')
	parser.add_argument ('forest', help='Local forest path')

	args = parser.parse_args()

	run = asyncio.ensure_future (pipeAsync (sys.stdin, args.forest))
	loop = asyncio.get_event_loop ()

	# connect signals
	stop = lambda signum: run.cancel ()
	for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGPIPE):
		loop.add_signal_handler (sig, stop, sig)

	try:
		loop.run_until_complete (run)
	except asyncio.CancelledError:
		pass
