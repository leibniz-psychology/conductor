# Copyright 2019–2020 Leibniz Institute for Psychology
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
	assert socketListener (socketServer) == [os.getpid ()]

@pytest.mark.asyncio
async def test_socketListener_missing ():
	with pytest.raises (KeyError):
		socketListener ('/nonexistent')

