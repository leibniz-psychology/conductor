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

import pytest

from .client import Client, parseSSHPath
from .test_util import socketServer

@pytest.mark.asyncio
async def test_checkSocketExists_yes (socketServer):
	with pytest.raises (FileExistsError):
		Client.checkSocketExists (socketServer)

def test_checkSocketExists_no ():
	Client.checkSocketExists ('/nonexistent')

def test_parseSSHPath ():
	assert parseSSHPath ('user@host:/path') == ('user', 'host', '/path')
	assert parseSSHPath ('user@host:/path@/path') == ('user', 'host', '/path@/path')
	assert parseSSHPath ('user@host:/path:/path') == ('user', 'host', '/path:/path')

	assert parseSSHPath ('host:/path') == (None, 'host', '/path')

