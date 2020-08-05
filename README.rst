Conductor
=========

conductor provides self-service application forwarding to a public web gateway
for compute cluster users.

.. figure:: conductor.png

	Overview

The basic flow is this: The web frontend exposed to the internet runs
``conductor-server``, which maintains a directory of configuration files and
sockets (the “forest”) that is world-writable. The user runs ``conductor`` on a
compute node, which then connects to the web frontend server via SSH, drops a
socket and configuration file into the forest and forwards any communication to
the actual application.

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

    conductor host:/var/forest app.socket -- my-application

and connect using the host/authorization pair returned, i.e.
``http://<urls[0]>/_conductor/auth/<auth>``.

Design considerations
---------------------

``conductor-server`` supports cookie-based authentication, but it should not be
exposed to the internet directly, because it only speaks a very limited subset
of HTTP. A reverse-proxy setup is highly recommended.

The forest exists to simplify connection multiplexing. If, for example, every
client would connect via a socket to the server, we’d have to multiplex
multiple incoming request streams over the same connection. Placing
connect()’able UNIX domain sockets in a directory allows a 1:1 mapping
for incoming to outgoing streams (and thus SSH handles multiplexing for us).

Proper permission handling on the forest directory is essential. Users must
create files with very strict permissions, in particular files must not be
world-readable or world-writable. Due to g+s being set on the forest directory
the group of created files is changed. Users cannot set the sticky bit on files
though if they are not members of this group. Thus they cannot setgid
executable files. Only the owner of a config file and socket can delete them
due to o+t.

