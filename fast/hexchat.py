#!/usr/bin/env python
import asyncore
import logging
import socket
import sleekxmpp
import sys
import os
import base64
import time
import threading

from sleekxmpp import Iq
from sleekxmpp.xmlstream.handler import Callback
from sleekxmpp.xmlstream.matcher import StanzaPath
from sleekxmpp.xmlstream import register_stanza_plugin
from sleekxmpp.util import bytes
from sleekxmpp.xmlstream import ElementBase

# Python versions before 3.0 do not use UTF-8 encoding
# by default. To ensure that Unicode is handled properly
# throughout SleekXMPP, we will set the default encoding
# ourselves to UTF-8.
if sys.version_info < (3, 0):
    reload(sys)
    sys.setdefaultencoding('utf8')
else:
    raw_input = input


class Initiate(ElementBase):
    name = 'initiate'
    namespace = 'https://www.torproject.org/transport/xmpp'
    plugin_attrib = 'tor_initiate'
    interfaces = set(('host', 'port'))
    sub_interfaces = interfaces

class InitiateSuccess(ElementBase):
    name = 'success'
    namespace = 'https://www.torproject.org/transport/xmpp'
    plugin_attrib = 'tor_initiate_success'
    interfaces = set(('sid',))

register_stanza_plugin(Iq, Initiate)
register_stanza_plugin(Iq, InitiateSuccess)

"""this class exchanges data between tcp sockets and xmpp servers."""
class bot(sleekxmpp.ClientXMPP):
    def __init__(self, jid, password, server):
        """
        Initialize a hexchat XMPP bot. Also connect to the XMPP server.

        'jid' is the login username@chatserver
        'password' is the password to login with
        """

        # <local_address> => <listening_socket> dictionary,
        # where 'local_address' is an IP:PORT string with the locallistening address,
        # and 'listening_socket' is the socket that listens and accepts connections on that address.
        self.server_sockets={}

        # <connection_id> => <xmpp_socket> dictionary,
        # where 'connection_id' is a tuple of the form:
        # (bound ip:port on client, xmpp username of server, ip:port server should forward data to)
        # and 'xmpp_socket' is a sleekxmpp socket that speaks to the XMPP bot on the other side.
        self.client_sockets={}

        #map is an opaque "socket map" used by asyncore
        self.map = {}

        #initialize the sleekxmpp client.
        sleekxmpp.ClientXMPP.__init__(self, jid, password)

        # gmail xmpp server is actually at talk.google.com
        if jid.find("@gmail.com")!=-1:
            self.connect_address = ("talk.google.com", 5222)
        else:
            self.connect_address = None

        #event handlers are sleekxmpp's way of dealing with important xml tags it recieves
        #the only unusual event handler here is the one for "message".
        #this is set to get_message and is used to filter data recieved over the chat server
        self.add_event_handler("session_start", self.session_start)
        self.add_event_handler("disconnected", self.disconnected)

        self.register_plugin('xep_0047', {
            'auto_accept': True
        })

        if server:
            self.add_event_handler("ibb_stream_start", self.stream_opened, threaded=True)
            self.add_event_handler("ibb_stream_data", self.stream_data)
            self.add_event_handler("ibb_stream_end", self.stream_end)
        else:
            self.add_event_handler("ibb_stream_data", self.client_stream_data)

        self.register_handler(Callback('Tor XMPP Transport Handler', StanzaPath('iq@type=set/tor_initiate'), self.handle_transport))

        #The scheduler is xmpp's multithreaded todo list
        #This line adds asyncore's loop to the todo list
        #It tells the scheduler to evaluate asyncore.loop(0.0, True, self.map, 1)
        self.scheduler.add("asyncore loop", 0.001, asyncore.loop, (0.0, True, self.map, 1), repeat=True)

        # Connect to XMPP server
        if self.connect(self.connect_address):
            self.process()
        else:
            raise(Exception(jid+" could not connect"))

    ### XMPP handling methods:

    def stream_end(self, stream):
        self.client_sockets["%s %s" % (stream.receiver, stream.sid)].close()


    def stream_opened(self, stream):
        """Called when an IBB request is received."""
        key = "%s %s" % (stream.receiver, stream.sid)
        logging.debug("Opened %s" % key)
        self.initiate_connection(key, stream)

    def stream_data(self, event):
        """Called when some IBB data received."""
        data = event['data']
        stream = event['stream']
        key = "%s %s" % (stream.receiver, stream.sid)

        self.client_sockets[key].buffer += data

    def client_stream_data(self, event):
        """Called when some IBB data on the listening side."""
        data = event['data']
        stream = event['stream']

        for k in self.client_sockets:
            if self.client_sockets[k].sid == stream.sid:
                self.client_sockets[k].buffer += data
                break

    def handle_transport(self, stanza):
        """Called when an iq in the https://www.torproject.org/transport/xmpp namespace is received."""
        port = int(stanza['tor_initiate']['port'])
        host = stanza['tor_initiate']['host']

        # threading.Thread(target=lambda: self.initiate_connection(stanza['from'], host, port)).start()

        #construct identifier for client_sockets
        sid = base64.b32encode(os.urandom(50))
        key = "%s %s" % (stanza['from'], sid)

        self.client_sockets[key] = asyncore.dispatcher(map=self.map)
        self.client_sockets[key].host = host
        self.client_sockets[key].port = port

        stanza.reply()
        stanza['tor_initiate_success']['sid'] = sid
        stanza.send()


    def session_start(self, event):
        """Called when the bot connects and establishes a session with the XMPP server."""

        # XMPP spec says that we should broadcast our presence when we connect.
        self.send_presence()

    def disconnected(self, event):
        """Called when the bot disconnects from the XMPP server.
        Try to reconnect.
        """

        logging.warning("XMPP chat server disconnected")
        logging.debug("Trying to reconnect")
        if self.connect(self.connect_address):
            logging.debug("connection reestabilshed")
        else:
            raise(Exception(jid+" could not connect"))

    ### Methods for connection/socket creation.

    def initiate_connection(self, key, stream):
        """Initiate connection to 'local_address' and add the socket to the client sockets map."""

        if not self.client_sockets[key].host:
            logging.warning("Invalid key: %s" % key)
            return

        self.client_sockets[key].stream = stream
        host = self.client_sockets[key].host
        port = self.client_sockets[key].port

        #pretend socket is already connected in case data is recieved while connecting
        self.client_sockets[key] = asyncore.dispatcher(map=self.map)
        #just some asyncore initialization stuff
        self.client_sockets[key].buffer=b''
        self.client_sockets[key].writable=lambda: False
        self.client_sockets[key].readable=lambda: False

        logging.debug("connecting to %s:%d" % (host, port))

        try: # connect to the ip:port
            connected_socket=socket.create_connection((host, port), 30)
        except (socket.error, OverflowError, ValueError) as e:
            logging.warning("could not connect to %s:%d: %s" % (host, port, e))
            del(self.client_sockets[key])
            return
            #if it could not connect, tell the bot on the the other side to disconnect
            # self.sendMessageWrapper(peer, local_address, remote_address, "disconnect me!", 'chat')

        # attach the socket to the appropriate client_sockets and fix asyncore methods
        self.client_sockets[key].set_socket(connected_socket)
        self.client_sockets[key].connected = True
        self.client_sockets[key].socket.setblocking(0)
        self.client_sockets[key].writable=lambda: len(self.client_sockets[key].buffer)>0
        self.client_sockets[key].handle_write=lambda: self.handle_write(key)
        self.client_sockets[key].readable=lambda: True
        self.client_sockets[key].handle_read=lambda: self.handle_read(key, stream)

    def add_client_socket(self, peer, remote_address, sock):
        """Add socket to the bot's routing table."""

        # Calculate the client_sockets identifier for this socket, and put in the dict.
        key=(peer,remote_address)
        self.client_sockets[key] = asyncore.dispatcher(sock, map=self.map)

        #just some asyncore initialization stuff
        self.client_sockets[key].buffer=b''
        self.client_sockets[key].writable=lambda: len(self.client_sockets[key].buffer)>0
        self.client_sockets[key].handle_write=lambda: self.handle_write(key)
        self.client_sockets[key].readable=lambda: False
        self.client_sockets[key].handle_close=lambda: self.handle_close(key)

    def add_server_socket(self, local_address, peer, remote_address, remote_port):
        """Create a listener and put it in the server_sockets dictionary."""

        portaddr_split=local_address.rfind(':')
        if portaddr_split == -1:
            raise(Exception("No port specified"+local_address))

        self.server_sockets[local_address] = asyncore.dispatcher(map=self.map)

        #just some asyncore initialization stuff
        self.server_sockets[local_address].create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sockets[local_address].writable=lambda: False
        self.server_sockets[local_address].set_reuse_addr()
        self.server_sockets[local_address].bind((local_address[:portaddr_split], int(local_address[portaddr_split+1:])))
        self.server_sockets[local_address].handle_accept = lambda: self.handle_accept(local_address, peer, remote_address, remote_port)
        self.server_sockets[local_address].listen(1023)

    ### asyncore callbacks:

    def handle_read(self, key, stream):
        """Called when a TCP socket has stuff to be read from it."""

        data = self.client_sockets[key].recv(8192)

        base64_data = base64.b64encode(data).decode("UTF-8")

        stream.sendall(data)

    def handle_write(self, key):
        """Called when a TCP socket has stuff to be written to it."""
        data=self.client_sockets[key].buffer
        self.client_sockets[key].buffer=self.client_sockets[key].buffer[self.client_sockets[key].send(data):]

    def handle_accept(self, local_address, peer, remote_address, remote_port):
        """Called when we have a new incoming connection to one of our listening sockets."""

        connection, local = self.server_sockets[local_address].accept()
        
        self.add_client_socket(peer, local, connection)

        iq = self.Iq()
        iq['type'] = 'set'
        iq['to'] = peer
        iq['tor_initiate']['host'] = remote_address
        iq['tor_initiate']['port'] = str(remote_port)
        resp = iq.send(block=False, callback=lambda x: self.handle_reply(peer, local, x))

    def handle_reply(self, peer, address, stanza):
        logging.debug("Reply: %s" % stanza)

        key = stanza['tor_initiate_success']['sid']

        stream = self['xep_0047'].open_stream(peer, block_size = 8192, sid = key, window = 20)

        self.client_sockets[(peer, address)].sid = key
        self.client_sockets[(peer, address)].stream = stream
        self.client_sockets[(peer, address)].readable=lambda: True
        self.client_sockets[(peer, address)].handle_read=lambda: self.handle_read((peer, address), stream)

    def handle_close(self, key):
        """Called when the TCP client socket closes."""

        if key in self.client_sockets:
            self.client_sockets[key].close()
            self.client_sockets[key].stream.close()
            del(self.client_sockets[key])
            peer, remote_address = key
            #send a disconnection request to the bot waiting on the other side of the xmpp server

    def handle_connect(self, client_socket, key):
        """Called when the TCP client socket connects successfully to destination."""

        local_address=key[0]
        logging.debug("connecting to "+local_address)
        client_socket.setblocking(0)
        #add the socket to bot's client_sockets
        self.client_sockets[key]=client_socket

if __name__ == '__main__':
    logging.basicConfig(filename=sys.argv[2],level=logging.DEBUG)
    if sys.argv[1]=="-c":
        if not len(sys.argv) in (5,10):
            raise(Exception("Wrong number of command line arguements"))
        else:
            username=sys.argv[3]
            password=sys.argv[4]
            bot0=bot(username, password, not len(sys.argv)==10)
            if len(sys.argv)==10:
                bot0.add_server_socket(sys.argv[5]+":"+sys.argv[6], sys.argv[7], sys.argv[8], int(sys.argv[9]))
    else:
        if len(sys.argv)!=3:
            raise(Exception("Wrong number of command line arguements"))
        bots={}
        fd=open(sys.argv[1])
        lines=fd.read().splitlines()
        fd.close()
        for line in lines:
            #if the line is of the form username@chatserver:password:
            if line[-1]==":":
                userpass_split=line.find(':')
                try:
                    username=line[:userpass_split]
                    bots[username]=bot(username, line[userpass_split+1:-1], True)
                    continue
                except IndexError:
                    raise(Exception("No password supplied."))
            [local_address, peer, remote_address]=line.split('==>')
            #add a server socket listening for incomming connections
            try:
                bots[username].add_server_socket(local_address, peer, remote_address)
            except (OverflowError, socket.error, ValueError) as msg:
                raise(msg)

    #program needs to be kept running on linux
    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            sys.exit(-1)
