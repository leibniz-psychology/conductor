Conductor
=========

conductor provides self-service application forwarding to a public web gateway
for compute cluster users.

.. figure:: conductor.png

	Overview: Orange line is flow from webbrowser to application, blue from
	user to conductor-server

The basic flow is this: The user runs ``conductor`` on a
compute node, which then connects to the web frontend server via SSH and further
connects to the ``conductor-server``’s client socket. The server will assign it
a directory to place its remote application socket into, which ``conductor``
will listen on. If a webbrowser connects to ``conductor-server`` it will
connect to the remote application socket, which is forwarded by the user’s
``conductor`` instance to the real application socket.

conductor is required, since a user must not bind his web application onto a
port on the compute machine for two reasons: a) It is usually not accessible to
him and b) other users on the same machine can access applications bound to
localhost as well. UNIX domain sockets respect filesystem permissions and thus
prevent b). Using SSH to forward connections is a well-established method for
a) and leverages existing authentication mechanisms.

Usage
-----

A guix package description is provided in ``contrib/conductor.scm``, which can
be activated using:

.. code:: console

    guix package -f contrib/conductor.scm

If you’re using systemd copy the service file:

.. code:: console

    cp contrib/conductor.service /etc/systemd/system/conductor.service

Then adjust the paths, add a user and group ``conductor`` and start it with
``systemctl enable conductor && systemctl start conductor``.

Finally a user can run software using

.. code:: console

    conductor host:2222/var/run/conductor/client app.socket -- my-application

and connect using the host/authorization pair returned, i.e.
``http://<urls[0]>/_conductor/auth/<auth>``.

Design considerations
---------------------

``conductor-server`` supports cookie-based authentication, but it should not be
exposed to the internet directly, because it only speaks a very limited subset
of HTTP. A reverse-proxy setup is highly recommended.

Dropping socket files into a directory is used to simplify connection
multiplexing. If we would use the connection to the client socket, we’d have to
multiplex multiple incoming request streams over the same connection. Placing
connect()’able UNIX domain sockets in a directory allows a 1:1 mapping for
incoming to outgoing streams (and thus SSH handles multiplexing for us). Using
a new non-guessable subdirectory with tight permissions for every new client
should make this reasonably safe.

