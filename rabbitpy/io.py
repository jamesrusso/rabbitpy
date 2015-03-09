"""
Core IO for rabbitpy

"""
import collections
import errno
import logging
try:
    import queue
except ImportError:
    import Queue as queue
import select
import socket
import ssl
import threading

LOGGER = logging.getLogger(__name__)

from pamqp import frame
from pamqp import exceptions as pamqp_exceptions
from pamqp import specification

from rabbitpy import base
from rabbitpy import events
from rabbitpy import exceptions

MAX_READ = specification.FRAME_MAX_SIZE
MAX_WRITE = specification.FRAME_MAX_SIZE
POLL_TIMEOUT = 3600.0


class SelectPoller(object):

    def __init__(self, fd, write_trigger):
        self.read = [[fd, write_trigger], [], [fd], POLL_TIMEOUT]
        self.write = [[fd, write_trigger], [fd], [fd], POLL_TIMEOUT]

    def poll(self, write_wanted):
        try:
            if write_wanted:
                rlist, wlist, xlist = select.select(*self.write)
            else:
                rlist, wlist, xlist = select.select(*self.read)
        except select.error:
            return [], [], []
        return ([sock.fileno() for sock in rlist],
                [sock.fileno() for sock in wlist],
                [sock.fileno() for sock in xlist])


class KQueuePoller(object):

    MAX_EVENTS = 1000

    def __init__(self, fd, write_trigger):
        self._fd = fd
        self._write_trigger = write_trigger
        self._kqueue = select.kqueue()
        self._kqueue.control([select.kevent(fd,
                                            select.KQ_FILTER_READ,
                                            select.KQ_EV_ADD)], 0)
        self._kqueue.control([select.kevent(write_trigger,
                                            select.KQ_FILTER_READ,
                                            select.KQ_EV_ADD)], 0)
        self._write_in_last_poll = False

    def poll(self, write_wanted):
        rlist, wlist, xlist = [], [], []
        try:
            events = self._kqueue.control(self._changelist(write_wanted),
                                          self.MAX_EVENTS, POLL_TIMEOUT)
        except select.error as error:
            LOGGER.debug('kqueue.control error: %s', error)
            return [], [], []
        for event in events:
            if event.filter == select.KQ_FILTER_READ:
                rlist.append(event.ident)
            elif event.filter == select.KQ_FILTER_WRITE:
                wlist.append(event.ident)
            if event.flags & select.KQ_EV_ERROR:
                xlist.append(event.ident)
            if event.flags & select.KQ_EV_EOF == select.KQ_EV_EOF:
                xlist.append(event.data)
        if xlist:
            self._cleanup()
        return rlist, wlist, xlist

    def _changelist(self, write_wanted):
        if write_wanted and not self._write_in_last_poll:
            self._write_in_last_poll = True
            return [select.kevent(self._fd,
                    select.KQ_FILTER_WRITE,
                    select.KQ_EV_ADD)]
        elif self._write_in_last_poll and not write_wanted:
            self._write_in_last_poll = False
            return [select.kevent(self._fd,
                                  select.KQ_FILTER_WRITE,
                                  select.KQ_EV_DELETE)]
        return None

    def _cleanup(self):
        self._kqueue.control([select.kevent(self._fd,
                                            select.KQ_FILTER_READ,
                                            select.KQ_EV_DELETE)], 0)
        self._kqueue.control([select.kevent(self._write_trigger,
                                            select.KQ_FILTER_READ,
                                            select.KQ_EV_DELETE)], 0)
        if self._write_in_last_poll:
            return [select.kevent(self._fd,
                                  select.KQ_FILTER_WRITE,
                                  select.KQ_EV_DELETE)]


class PollPoller(object):

    # Register constants to prevent platform specific errors
    POLLIN = 1
    POLLOUT = 4
    POLLERR = 8

    READ = POLLIN | POLLERR
    WRITE = POLLIN | POLLOUT | POLLERR

    def __init__(self, fd, write_trigger):
        self._fd = fd
        self._poll = select.poll()
        self._poll.register(fd, self.READ)
        self._poll.register(write_trigger, self.READ)
        self._write_in_last_poll = False

    def _update_poll(self, write_wanted):
        if self._write_in_last_poll:
            if not write_wanted:
                self._write_in_last_poll = False
                self._poll.modify(self._fd, self.READ)
        else:
            self._write_in_last_poll = True
            self._poll.modify(self._fd, self.WRITE)

    def poll(self, write_wanted):
        self._update_poll(write_wanted)
        rlist, wlist, xlist = [], [], []
        try:
            events = self._poll.poll(POLL_TIMEOUT * 1000)
        except select.error:
            return [], [], []
        for fileno, event in events:
            if event & self.POLLIN:
                rlist.append(fileno)
            if event & self.POLLOUT:
                wlist.append(fileno)
            if event & self.POLLERR:
                xlist.append(fileno)
        return rlist, wlist, xlist


class IOLoop(object):

    def __init__(self, fd, error_callback, read_callback, write_queue,
                 event_obj, write_trigger, exception_stack):
        self._data = threading.local()
        self._data.fd = fd
        self._data.error_callback = error_callback
        self._data.read_callback = read_callback
        self._data.running = False
        self._data.ssl = hasattr(fd, 'read')
        self._data.events = event_obj
        self._data.write_buffer = collections.deque()
        self._data.write_queue = write_queue
        self._data.write_trigger = write_trigger
        self._exceptions = exception_stack
        self._poller = self._create_poller(fd, write_trigger)

    @staticmethod
    def _create_poller(fd, write_trigger):
        if hasattr(select, 'poll'):
            LOGGER.debug('Returning PollPoller')
            return PollPoller(fd, write_trigger)
        if hasattr(select, 'kqueue'):
            LOGGER.debug('Returning KQueuePoller')
            return KQueuePoller(fd, write_trigger)
        else:
            LOGGER.debug('Returning SelectPoller')
            return SelectPoller(fd, write_trigger)

    def run(self):
        self._data.running = True
        while self._data.running:
            try:
                self._poll()
            except EnvironmentError as exception:
                if (getattr(exception, 'errno', None) == errno.EINTR or
                    (isinstance(getattr(exception, 'args', None), tuple) and
                     len(exception.args) == 2 and
                     exception.args[0] == errno.EINTR)):
                    continue
            if self._data.events.is_set(events.SOCKET_CLOSE):
                LOGGER.debug('Exiting due to closed socket')
                break
        LOGGER.debug('Exiting IOLoop.run')

    def stop(self):
        LOGGER.debug('Stopping IOLoop')
        self._data.running = False
        try:
            self._data.write_trigger.close()
        except socket.error:
            pass

    def _poll(self):
        # Poll select with the materialized lists
        if not self._data.running:
            LOGGER.debug('Exiting poll')

        # Build the outbound write buffer of marshalled frames
        while not self._data.write_queue.empty():
            data = self._data.write_queue.get(False)
            self._data.write_buffer.append(frame.marshal(data[1], data[0]))

        # Poll the poller, passing in a bool if there is data to write
        rlist, wlist, xlist = self._poller.poll(bool(self._data.write_buffer))

        if xlist:
            LOGGER.debug('Poll errors: %r', xlist)
            self._data.events.set(events.SOCKET_CLOSE)
            self._data.error_callback('Connection reset')
            return

        # Clear out the trigger socket
        if self._data.write_trigger.fileno() in rlist:
            self._data.write_trigger.recv(1024)

        # Read if the data socket is in the read list
        if self._data.fd.fileno() in rlist:
            self._read()

        # Write if the data socket is writable
        if wlist and self._data.write_buffer:
            self._write()

    def _read(self):
        if not self._data.running:
            LOGGER.debug('Skipping read, not running')
            return
        try:
            if self._data.ssl:
                self._data.read_callback(self._data.fd.read(MAX_READ))
            else:
                self._data.read_callback(self._data.fd.recv(MAX_READ))
        except socket.timeout:
            LOGGER.warning('Timed out reading from socket')
        except socket.error as exception:
            self._data.running = False
            self._data.error_callback(exception)

    def _write(self):
        if not self._data.running:
            LOGGER.debug('Skipping write frame, not running')
            return

        frame_data = self._data.write_buffer.popleft()
        try:
            bytes_sent = self._data.fd.send(frame_data)
        except socket.timeout:
            LOGGER.warning('Timed out writing %i bytes to socket',
                           len(frame_data))
            self._data.write_buffer.appendleft(frame_data)
        except socket.error as error:
            if error.errno == 35:
                LOGGER.debug('socket resource temp unavailable')
                self._data.write_buffer.appendleft(frame_data)
            else:
                self._data.running = False
                self._data.error_callback(error)
        else:
            # If the entire frame could not be send, send the rest next time
            if bytes_sent < len(frame_data):
                self._data.write_buffer.appendleft(frame_data[bytes_sent:])


class IO(threading.Thread, base.StatefulObject):

    CONNECTION_TIMEOUT = 3
    CONTENT_METHODS = ['Basic.Deliver', 'Basic.GetOk', 'Basic.Return']
    READ_BUFFER_SIZE = specification.FRAME_MAX_SIZE
    SSL_KWARGS = {'keyfile': 'keyfile',
                  'certfile': 'certfile',
                  'cert_reqs': 'verify',
                  'ssl_version': 'ssl_version',
                  'cacerts': 'cacertfile'}

    def __init__(self, group=None, target=None, name=None, args=(),
                 kwargs=None):
        if kwargs is None:
            kwargs = dict()
        super(IO, self).__init__(group, target, name, args, kwargs)

        self._args = kwargs['connection_args']
        self._events = kwargs['events']
        self._exceptions = kwargs['exceptions']
        self._write_queue = kwargs['write_queue']

        # A socket to trigger write interrupts with
        self._write_listener, self._write_trigger = self._socketpair()

        self._buffer = bytes()
        self._lock = threading.RLock()
        self._channels = dict()
        self._remote_name = None
        self._socket = None
        self._state = None
        self._loop = None

    def add_channel(self, channel, write_queue):
        """Add a channel to the channel queue dict for dispatching frames
        to the channel.

        :param rabbitpy.channel.Channel: The channel to add
        :param Queue.Queue write_queue: Queue for sending frames to the channel

        """
        self._channels[int(channel)] = channel, write_queue

    def run(self):
        """

        """
        self._connect()
        if not self._socket:
            LOGGER.warning('Could not connect to %s:%s',
                           self._args['host'], self._args['port'])
            return

        LOGGER.debug('Socket connected')

        # Create the remote name
        local_socket = self._socket.getsockname()
        peer_socket = self._socket.getpeername()
        self._remote_name = '%s:%s -> %s:%s' % (local_socket[0],
                                                local_socket[1],
                                                peer_socket[0],
                                                peer_socket[1])
        self._loop = IOLoop(self._socket, self.on_error, self.on_read,
                            self._write_queue,
                            self._events,
                            self._write_listener,
                            self._exceptions)
        self._loop.run()
        if not self._exceptions.empty() and \
                not self._events.is_set(events.EXCEPTION_RAISED):
            self._events.set(events.EXCEPTION_RAISED)
        LOGGER.debug('Exiting IO.run')

    def on_error(self, exception):
        """Common functions when a socket error occurs. Make sure to set closed
        and add the exception, and note an exception event.

        :param socket.error exception: The socket error

        """
        args = [self._args['host'], self._args['port'], str(exception)]
        if self._channels[0][0].open:
            self._exceptions.put(exceptions.ConnectionResetException(*args))
        else:
            self._exceptions.put(exceptions.ConnectionException(*args))
        self._events.set(events.EXCEPTION_RAISED)

    def on_read(self, data):
        # Append the data to the buffer
        self._buffer += data

        while self._buffer:

            # Read and process data
            value = self._read_frame()

            # Break out if a frame could not be decoded
            if self._buffer and value[0] is None:
                break

            # LOGGER.debug('Received (%i) %r', value[0], value[1])

            # If it's channel 0, call the Channel0 directly
            if value[0] == 0:
                self._lock.acquire(True)
                self._channels[0][0].on_frame(value[1])
                self._lock.release()
                continue

            self._add_frame_to_queue(value[0], value[1])

    def stop(self):
        """Stop the IO Layer due to exception or other problem.

        """
        self._close()

    @property
    def write_trigger(self):
        return self._write_trigger

    def _add_frame_to_queue(self, channel_id, frame_value):
        """Add the frame to the stack by creating the key value used in
        expectations and then add it to the list.

        :param int channel_id: The channel id the frame was received on
        :param pamqp.specification.Frame frame_value: The frame to add

        """
        # LOGGER.debug('Adding %s to channel %s', frame_value.name, channel_id)
        self._channels[channel_id][1].put(frame_value)

    def _close(self):
        self._set_state(self.CLOSING)
        if hasattr(self, '_socket') and self._socket:
            self._socket.close()
        self._events.clear(events.SOCKET_OPENED)
        self._events.set(events.SOCKET_CLOSED)
        self._set_state(self.CLOSED)

    def _connect_socket(self, sock, address):
        """Connect the socket to the specified host and port."""
        LOGGER.debug('Connecting to %r', address)
        sock.settimeout(self.CONNECTION_TIMEOUT)
        sock.connect(address)

    def _connect(self):
        """Connect to the RabbitMQ Server

        :raises: ConnectionException

        """
        self._set_state(self.OPENING)
        sock = None
        for (af, socktype, proto,
             canonname, sockaddr) in self._get_addr_info():
            try:
                sock = self._create_socket(af, socktype, proto)
                self._connect_socket(sock, sockaddr)
                break
            except socket.error as error:
                LOGGER.debug('Error connecting to %r: %s', sockaddr, error)
                sock = None
                continue

        if not sock:
            args = [self._args['host'], self._args['port'],
                    'Could not connect']
            self._exceptions.put(exceptions.ConnectionException(*args))
            self._events.set(events.EXCEPTION_RAISED)
            return

        self._socket = sock
        self._socket.settimeout(0)
        self._events.set(events.SOCKET_OPENED)
        self._set_state(self.OPEN)

    def _create_socket(self, af, socktype, proto):
        """Create the new socket, optionally with SSL support.

        :rtype: socket.socket or ssl.SSLSocket

        """
        sock = socket.socket(af, socktype, proto)
        if self._args['ssl']:
            kwargs = {'sock': sock,
                      'server_side': False}
            for argv, key in self.SSL_KWARGS.iteritems():
                if self._args[key]:
                    kwargs[argv] = self._args[key]
            LOGGER.debug('Wrapping socket for SSL: %r', kwargs)
            return ssl.wrap_socket(**kwargs)
        return sock

    def _disconnect_socket(self):
        """Close the existing socket connection"""
        self._socket.close()

    def _get_addr_info(self):
        family = socket.AF_UNSPEC
        if not socket.has_ipv6:
            family = socket.AF_INET
        try:
            res = socket.getaddrinfo(self._args['host'],
                                     self._args['port'],
                                     family,
                                     socket.SOCK_STREAM,
                                     0)
        except socket.error as error:
            LOGGER.error('Could not resolve %s: %s',
                         self._args['host'], error, exc_info=True)
            return []
        return res

    @staticmethod
    def _get_frame_from_str(value):
        """Get the pamqp frame from the string value.

        :param str value: The value to parse for an pamqp frame
        :return (str, int, pamqp.specification.Frame): Remainder of value,
                                                       channel id and
                                                       frame value
        """
        if not value:
            return value, None, None
        try:
            byte_count, channel_id, frame_in = frame.unmarshal(value)
        except pamqp_exceptions.UnmarshalingException:
            return value, None, None
        except specification.AMQPFrameError as error:
            LOGGER.error('Failed to demarshal: %r', error, exc_info=True)
            LOGGER.debug(value)
            return value, None, None
        return value[byte_count:], channel_id, frame_in

    def _notify_of_basic_return(self, channel_id, frame_value):
        """Invoke the on_basic_return code in the specified channel. This will
        block the IO loop unless the exception is caught.

        :param int channel_id: The channel for the basic return
        :param pamqp.specification.Frame frame_value: The Basic.Return frame

        """
        self._channels[channel_id][0].on_basic_return(frame_value)

    def _read_frame(self):
        """Read from the buffer and try and get the demarshaled frame.

        :rtype (int, pamqp.specification.Frame): The channel and frame

        """
        self._buffer, chan_id, value = self._get_frame_from_str(self._buffer)
        return chan_id, value

    def _remote_close_channel(self, channel_id, frame_value):
        """Invoke the on_channel_close code in the specified channel. This will
        block the IO loop unless the exception is caught.

        :param int channel_id: The channel to remote close
        :param pamqp.specification.Frame frame_value: The Channel.Close frame

        """
        self._channels[channel_id][0].on_remote_close(frame_value)

    def _socketpair(self):
        """Return a socket pair regardless of platform.

        :rtype: (socket, socket)

        """
        try:
            server, client = socket.socketpair()
        except AttributeError:
            # Connect in Windows
            LOGGER.debug('Falling back to emulated socketpair behavior')

            # Create the listening server socket & bind it to a random port
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('127.0.0.1', 0))

            # Get the port for the notifying socket to connect to
            port = s.getsockname()[1]

            # Create the notifying client socket and connect using a timer
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            def connect():
                client.connect(('127.0.0.1', port))

            t = threading.Timer(0.01, connect)
            t.start()

            # Have the listening server socket listen and accept the connect
            s.listen(0)
            server, _unused = s.accept()

        # Don't block on either socket
        server.setblocking(0)
        client.setblocking(0)
        return server, client

    def _trigger_write(self):
        """Notifies the IO loop we need to write a frame by writing a byte
        to a local socket.

        """
        try:
            self._write_trigger.send(b'0')
        except socket.error:
            pass
