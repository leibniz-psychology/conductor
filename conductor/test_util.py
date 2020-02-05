import pytest, asyncio, sys, os, tempfile
from .util import socketListener, listSockets, listProcesses

async def cb (reader, writer):
	l = await reader.readline ()
	writer.write (l)

@pytest.fixture
async def socketServer ():
	socketpath = tempfile.mktemp (suffix='.socket')
	server = await asyncio.start_unix_server (cb, socketpath)
	task = asyncio.ensure_future (server.serve_forever ())
	yield socketpath
	task.cancel ()
	server.close ()
	os.unlink (socketpath)

@pytest.mark.asyncio
async def test_socketServer (socketServer):
	sockreader, sockwriter = await asyncio.open_unix_connection (path=socketServer)
	sockwriter.write (b'test\n')
	data = await sockreader.readline ()
	assert data == b'test\n'
	sockwriter.close ()

@pytest.mark.asyncio
async def test_listSockets (socketServer):
	found = False
	for s in listSockets ():
		if s.path == socketServer:
			found = True
	assert found

@pytest.mark.asyncio
async def test_listProcesses (socketServer):
	found = False
	mypid = os.getpid ()
	for pid, files in listProcesses ():
		if pid == 1:
			# should not be able to read fds
			assert not files
		elif pid == mypid:
			assert files
			found = True
	assert found

@pytest.mark.asyncio
async def test_socketListener_exist (socketServer):
	assert socketListener (socketServer) == os.getpid ()

@pytest.mark.asyncio
async def test_socketListener_missing ():
	with pytest.raises (KeyError):
		socketListener ('/nonexistent')

