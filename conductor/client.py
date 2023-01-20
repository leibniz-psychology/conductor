# Copyright 2019–2022 Leibniz Institute for Psychology
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import asyncio, argparse, sys, json, os, signal, secrets, subprocess, traceback
from enum import unique, IntEnum
from hashlib import blake2b

import asyncssh
from furl import furl
import structlog

from .util import proxy, socketListener, configureStructlog

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

	def __init__ (self, localSocket, controlUrl, command, token, replace=False, key=None):
		self.controlUrl = controlUrl
		self.localSocket = localSocket
		self.command = command
		self.token = token
		self.replace = replace
		self.key = key
		self.nextConnId = 0
		self.logger = structlog.get_logger ()

	def connectLocal (self):
		return asyncio.open_unix_connection (path=self.localSocket)

	async def handler (self, reader, writer):
		connid = self.nextConnId
		self.nextConnId += 1
		logger = self.logger.bind (connid=connid)
		logger.debug ('client_connect')

		try:
			sockreader, sockwriter = await self.connectLocal ()
		except Exception as e:
			logger.error ('client_socket_unavailable', error=e)
			writer.close ()
			return

		await proxy ((sockreader, sockwriter, 'sock'), (reader, writer, 'ssh'), logger=logger)

	def accept (self):
		return self.handler

	@staticmethod
	def checkSocketExists (path, logger, replace=False):
		if os.path.exists (path):
			try:
				for pid in socketListener (path):
					if replace:
						logger.info ('replace_pid', pid=pid)
						os.kill (pid, signal.SIGTERM)
					else:
						logger.error ('existing_pid', pid=pid)
						raise FileExistsError ()
			except KeyError:
				logger.debug ('stale_socket_unlink', path=path)
				os.unlink (path)

	def connect (self):
		""" Connect to control SSH server """
		controlUrl = self.controlUrl
		# remove null arguments
		connectArgs = dict (filter (lambda x: x[1], [('host', controlUrl.host), ('port', controlUrl.port), ('username', controlUrl.username)]))
		knownHostsFile = '/etc/ssh/ssh_known_hosts'
		if os.path.exists (knownHostsFile):
			connectArgs['options'] = asyncssh.SSHClientConnectionOptions (known_hosts=knownHostsFile)
		return asyncssh.connect (**connectArgs)

	async def runControl (self, programReady):
		""" Connection to conductor-server """

		logger = self.logger

		async def openReaderWriter ():
			while True:
				try:
					logger.debug ('control_connect')
					return await conn.open_unix_connection (str (controlUrl.path),
							encoding='utf-8')
				except asyncssh.misc.ChannelOpenError:
					await asyncio.sleep (0.1)

		connectTotalTimeout = 3
		controlUrl = self.controlUrl
		async with self.connect () as conn:
			writeJson (dict (state='connect', user=controlUrl.username,
					host=controlUrl.host, port=controlUrl.port))

			tries = 0
			while tries < 5:
				tries += 1

				try:
					controlReader, controlWriter = await asyncio.wait_for (
							openReaderWriter (), timeout=connectTotalTimeout)
				except asyncio.TimeoutError:
					logger.error ('control_connect_timeout', timeout=connectTotalTimeout)
					# fatal error
					return False
				# if the initial connection succeeded we can wait longer for it.
				connectTotalTimeout = 60

				l = await controlReader.readline ()
				banner = json.loads (l)
				writeJson (dict (state='controlConnect', path=str (controlUrl.path), banner=banner))

				try:
					remoteSocketPath = os.path.join (banner['directory'], 'socket')
					listener = await conn.start_unix_server (self.accept,
							listen_path=remoteSocketPath)
				except asyncssh.misc.ChannelListenError as e:
					logger.error ('control_listen_error', error=e)
					# this is a hard error
					return False

				# fix permissions on the socket, so server can read/write
				chmodProc = await conn.create_process (f'chmod 660 {remoteSocketPath}')
				await chmodProc.wait ()

				await programReady.wait ()

				config = {'auth': self.token}
				if self.key:
					config['key'] = self.key
				config['auth'] = blake2b (self.token.encode ('utf-8')).hexdigest ()
				controlWriter.write (json.dumps (config) + '\n')
				await controlWriter.drain ()

				l = await controlReader.readline ()
				resp = json.loads (l)
				if resp.get ('status') != 'ok':
					writeJson (dict (state='failed', reason=resp['status']))
					return ExitCode.ERROR
				writeJson (dict (state='live', config=dict (auth=self.token, key=self.key, urls=resp['urls'])))

				# reset tries
				tries = 0
				while True:
					l = await controlReader.readline ()
					if not l:
						break
				writeJson (dict (state='dead'))

	@staticmethod
	async def wrapFd (fd, kind):
		""" Wrap data from fd into json messages """
		bufsize = 256*1024
		while True:
			buf = await fd.read (bufsize)
			if not buf:
				break
			writeJson (dict (
					state='data',
					kind=kind,
					data=buf.decode ('utf-8', errors='replace'),
					))

	async def runProgram (self, programReady):
		""" Run the client program """

		logger = self.logger.bind (localSocket=self.localSocket, command=self.command)

		logger.debug ('program_start')
		commandproc = await asyncio.create_subprocess_exec (self.command[0],
				*self.command[1:], start_new_session=True,
				stdout=subprocess.PIPE, stderr=subprocess.PIPE)
		try:
			stdoutTask = asyncio.create_task (self.wrapFd (commandproc.stdout, 'stdout'))
			stderrTask = asyncio.create_task (self.wrapFd (commandproc.stderr, 'stderr'))
			writeJson (dict (state='execute', command=self.command, pid=commandproc.pid))

			# wait for the application to become live, i.e. socket exists and server responds
			while True:
				try:
					await self.connectLocal ()
					logger.debug ('program_ready')
					break
				except Exception as e:
					logger.debug ('program_waiting', error=e)
					if commandproc.returncode is not None:
						writeJson (dict (state='failed', reason='subprocess terminated'))
						return ExitCode.ERROR
					await asyncio.sleep (0.1)
			
			programReady.set ()

			return await commandproc.wait ()
		except asyncio.CancelledError:
			logger.debug ('program_cancelled')
		except Exception:
			raise
		finally:
			ret = commandproc.returncode
			if ret is None:
				logger.debug ('program_terminate')
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

			for task in (stdoutTask, stderrTask):
				task.cancel ()
				try:
					await task
				except asyncio.CancelledError:
					pass

	async def run (self):
		logger = self.logger

		try:
			self.checkSocketExists (self.localSocket, logger, self.replace)
		except FileExistsError:
			return ExitCode.SOCKET_USED

		logger.debug ('starting')
		programReady = asyncio.Event ()
		runProgramTask = asyncio.ensure_future (self.runProgram (programReady))
		runControlTask = asyncio.ensure_future (self.runControl (programReady))

		try:
			logger.debug ('waiting')
			# We’re done if either of the tasks exits.
			done, pending = await asyncio.wait (
					[runProgramTask, runControlTask],
					return_when=asyncio.FIRST_COMPLETED)
			logger.debug ('future_terminated', done=done, pending=pending)
		except asyncio.CancelledError:
			logger.debug ('cancelled')
			return ExitCode.OK
		except Exception as e:
			logger.error ('error', error=e)
			return ExitCode.ERROR
		else:
			if runControlTask.done ():
				# This is always an error
				return ExitCode.ERROR
			elif runProgramTask.done ():
				# check return code
				try:
					ret = runProgramTask.result ()
					if ret == 0:
						return ExitCode.OK
				except Exception as e:
					pass
				return ExitCode.ERROR
			else:
				# not reached
				assert False
		finally:
			logger.debug ('cancel_tasks')
			runProgramTask.cancel ()
			runControlTask.cancel ()
			await runProgramTask
			await runControlTask

def main (): # pragma: nocover
	parser = argparse.ArgumentParser(description='conductor client')
	parser.add_argument ('-r', '--replace', action='store_true', help='Replace existing process')
	parser.add_argument ('-v', '--verbose', action='store_true', help='Verbose output')
	parser.add_argument ('-k', '--key', default='', help='Subdomain')
	parser.add_argument ('server', type=lambda x: furl ('unix://' + x),
			help='Server to connect to. Supports URLs like user@host:port/path')
	parser.add_argument ('localSocket', help='Local socket to connect to')
	parser.add_argument ('command', nargs=argparse.REMAINDER, help='Command to run')

	args = parser.parse_args()

	configureStructlog (args.verbose)

	token = os.getenv ('CONDUCTOR_TOKEN', None)
	if token is not None:
		# make sure the token does not spill into any subprocesses we start
		os.unsetenv ('CONDUCTOR_TOKEN')
	else:
		# generate a random one, if none supplied
		token = randomSecret (32)

	server = args.server
	if not server.path:
		server = server.set (path='/var/run/conductor/client')
	client = Client (localSocket=args.localSocket, controlUrl=args.server,
			command=args.command, token=token, replace=args.replace, key=args.key)

	run = asyncio.ensure_future (client.run ())
	loop = asyncio.get_event_loop ()
	stop = lambda signum: run.cancel ()
	for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
		loop.add_signal_handler (sig, stop, sig)
	try:
		return loop.run_until_complete (run)
	except asyncio.CancelledError:
		return ExitCode.OK
	except Exception as e:
		traceback.print_exc (file=sys.stderr)
		return ExitCode.ERROR

