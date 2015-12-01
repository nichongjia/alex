#!/usr/bin/env python
# -*- coding: utf-8 -*-
import select
import multiprocessing
import threading
from twisted.internet import reactor
import random
import string

from autobahn.twisted.websocket import WebSocketServerProtocol, WebSocketServerFactory, WebSocketClientFactory, WebSocketClientProtocol

from alex.components.hub.messages import Command, Frame, ASRHyp, TTSText
from alex.components.hub.voiceio import VoiceIO
from wsio_messages_pb2 import ClientToAlex, AlexToClient, WSRouterRequestProto, PingProto


class WSIO(VoiceIO, multiprocessing.Process):
    """
    WebSocket IO.
    """
    AUDIO_FRAMES_PER_MSG = 512

    def __init__(self, cfg, commands, audio_record, audio_play, close_event):
        """ Initialize WebIO

        cfg - configuration dictionary

        audio_record - inter-process connection for sending recorded audio.
          Audio is divided into frames, each with the length of samples_per_frame.

        audio_play - inter-process connection for receiving audio which should to be played.
          Audio must be divided into frames, each with the length of samples_per_frame.

        """
        super(WSIO, self).__init__(cfg, commands, audio_record, audio_play, close_event)

        self.cfg = cfg
        self.commands = commands
        self.audio_record = audio_record
        self.audio_play = audio_play
        self.close_event = close_event

        self.router_address = self.cfg['WSIO']['router_addr']
        self.router_port = self.cfg['WSIO']['router_port']

        self.listen_address = self.cfg['WSIO']['listen_addr']
        self.listen_port = self.cfg['WSIO']['listen_port']

        self.alex_addr = self.cfg['WSIO']['alex_addr']

        self.reset()

    def reset(self):
        super(WSIO, self).reset()

        self.audio_to_send = ""
        self.client_connected = False

        self.audio_playing = None
        self.n_sent_frames = None
        self.curr_seq = 0
        self.utterance_id = -1
        self.open_utterance_id = -1

    def process_pending_commands(self):
        """Process all pending commands.

        Available commands:
          stop() - stop processing and exit the process
          flush() - flush input buffers.
            Now it only flushes the input connection.
            It is not able flush data already send to the sound card.

        Return True if the process should terminate.
        """

        if self.commands.poll():
            command = self.commands.recv()
            if self.cfg['AudioIO']['debug']:
                self.cfg['Logging']['system_logger'].debug(command)

            if isinstance(command, Command):
                if command.parsed['__name__'] == 'stop':
                    # Discard all data in play buffer.
                    while self.audio_play.poll():
                        self.audio_play.recv()

                    return True

                if command.parsed['__name__'] == 'flush':
                    # Discard all data in play buffer.
                    while self.audio_play.poll():
                        self.audio_play.recv()

                    return False

                if command.parsed['__name__'] == 'flush_out':
                    self.audio_to_send = ""
                    while self.audio_play.poll():
                        self.audio_play.recv()

                    msg = AlexToClient()
                    msg.type = AlexToClient.FLUSH_OUT_AUDIO
                    msg.priority = -1
                    msg.seq = self.curr_seq
                    self.send_to_client(msg.SerializeToString())

                if command.parsed['__name__'] == 'reset':
                    self.reset()

            elif isinstance(command, ASRHyp):
                hyp = command.hyp
                asr_hyp = hyp.get_best()

                msg = AlexToClient()
                msg.type = AlexToClient.ASR_RESULT
                msg.asr_result = unicode(asr_hyp).lower()
                msg.priority = -1
                msg.seq = self.curr_seq
                self.send_to_client(msg.SerializeToString())
            elif isinstance(command, TTSText):
                txt = command.text

                msg = AlexToClient()
                msg.type = AlexToClient.SYSTEM_PROMPT
                msg.priority = -1
                msg.seq = self.curr_seq
                msg.system_prompt = unicode(txt)
                self.send_to_client(msg.SerializeToString())

        return False

    def read_write_audio(self):
        """Send some of the available data to the output.
        It should be a non-blocking operation.

        Therefore:
          1) do not send more then play_buffer_frames
          2) send only if stream.get_write_available() is more then the frame size
        """

        if self.audio_play.poll():
            audio_play_msg = self.audio_play.recv()
            if not self.client_connected:
                return

            if isinstance(audio_play_msg, Frame) and self.open_utterance_id != -1:
                buffer = audio_play_msg.payload

                self.audio_to_send += buffer
                self.n_sent_frames += len(buffer) / 2

                self.process_frame(audio_play_msg, self.open_utterance_id)

            elif isinstance(audio_play_msg, Command):
                if audio_play_msg.parsed['__name__'] == 'utterance_start':
                    if self.open_utterance_id != -1:
                        self._send_speech_info(end=True)
                        self.process_utt_end(audio_play_msg, self.open_utterance_id)

                    self.utterance_id += 1
                    self.open_utterance_id = self.utterance_id

                    self._send_speech_info(end=False)
                    self.process_utt_start(audio_play_msg, self.open_utterance_id)

                if audio_play_msg.parsed['__name__'] == 'utterance_end' and self.open_utterance_id != -1:
                    self._send_speech_info(end=True)
                    self.process_utt_end(audio_play_msg, self.open_utterance_id)
                    self.open_utterance_id = -1


        while len(self.audio_to_send) > WSIO.AUDIO_FRAMES_PER_MSG:
            buffer = self.audio_to_send[:WSIO.AUDIO_FRAMES_PER_MSG]
            self.audio_to_send = self.audio_to_send[WSIO.AUDIO_FRAMES_PER_MSG:]

            msg = AlexToClient()
            msg.type = AlexToClient.SPEECH
            msg.speech = buffer
            msg.seq = self.curr_seq
            self.send_to_client(msg.SerializeToString())

            # self.cfg['Logging']['system_logger'].info('Sent SPEECH to Android: seq=%d buff_len=%d' % (msg.seq, len(buffer)))

    def _send_speech_info(self, end=False):
        msg = AlexToClient()
        msg.seq = self.curr_seq
        if end:
            msg.type = AlexToClient.SPEECH_END
        else:
            msg.type = AlexToClient.SPEECH_BEGIN
        msg.utterance_id = self.open_utterance_id
        self.send_to_client(msg.SerializeToString())

        self.cfg['Logging']['system_logger'].info('Sent SPEECH_INFO to Android: end=%s utt=%d' % (end, self.open_utterance_id))


    def run(self):
        try:
            self.cfg['Logging']['session_logger'].cancel_join_thread()

            global logger
            logger = self.cfg['Logging']['system_logger']

            self.key = self._gen_client_key()

            self._start_websocket_server()

            while 1:
                select.select([self.commands, self.audio_play], [], [], 1.0)

                if self.close_event.is_set():
                    return

                if self.process_pending_commands():
                    return

                self.read_write_audio()
        except:
            self.cfg['Logging']['system_logger'].exception('Uncaught exception in VAD process.')
            self.close_event.set()
            raise

    def _start_websocket_server(self):
        # Initialize the listening connection where the clients can connect.
        ws_server_factory = AlexServerFactory(self.listen_address, self.listen_port, self)
        reactor.listenTCP(self.listen_port, ws_server_factory)

        # Initialize connection for communication with a WSRouter.
        ws_ping_factory = AlexPingFactory(self.router_address, self.router_port, self)
        reactor.connectTCP(self.router_address, self.router_port, ws_ping_factory)

        # Run the Twisted reactor on a thread.
        conns = threading.Thread(target=reactor.run, kwargs=dict(installSignalHandlers=0))
        conns.setDaemon(True)
        conns.start()

    def _gen_client_key(self):
        """Generate a random key for client identification.

        This key is passed by a WSRouter to the clients so that they prove that they connected to Alex through the
        WSRouter."""
        return ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(20))

    def on_client_connected(self, protocol, request):
        """Run when a new client connect."""
        self.commands.send(Command('incoming_call(remote_uri="%s")' % "PubAlex", 'WSIO', 'HUB'))
        self.commands.send(Command('call_confirmed(remote_uri="%s")' % "PubAlex", 'WSIO', 'HUB'))
        self.ws_protocol = protocol
        self.client_connected = True
        self.n_sent_frames = 0

    def on_client_closed(self):
        """Run when the current client disconnects."""
        self.ws_protocol = None
        self.commands.send(Command('call_disconnected(remote_uri="%s", code="%s")' % ("PubAlex", "---"), 'WSIO', 'HUB'))
        self.client_connected = False
        self.key = self._gen_client_key()

    def on_client_message_received(self, payload):
        if not self.client_connected:
            return

        msg = ClientToAlex()
        msg.ParseFromString(payload)
        if msg.key == self.key:
            decoded = msg.speech

            try:
                self.update_current_utterance_id(msg.currently_playing_utterance)
            except Exception, e:
                self.cfg['Logging']['system_logger'].warning("Exception while setting current utterance ID:")
                self.cfg['Logging']['system_logger'].exception(e)

            self.audio_record.send(Frame(decoded))

    def send_to_client(self, data):
        """Send given data to the client."""
        self.curr_seq += 1
        if self.ws_protocol:
            reactor.callFromThread(self.ws_protocol.sendMessage, data, True)
        else:
            self.cfg['Logging']['system_logger'].warning("Send to client called but the connection is not opened.")

    def build_ping_message(self):
        msg = WSRouterRequestProto()
        msg.type = WSRouterRequestProto.PING
        if self.client_connected:
            msg.ping.status = PingProto.BUSY
        else:
            msg.ping.status = PingProto.AVAILABLE

        msg.ping.key = self.key
        msg.ping.addr = self.alex_addr

        return msg


class AlexServerFactory(WebSocketServerFactory):
    """Twisted Factory that takes care of instantiating AlexWebsocketProtocols."""
    def __init__(self, addr, port, wsio):
        super(AlexServerFactory, self).__init__("ws://%s:%d" % (addr, port), debug=True)
        self.protocol = AlexServerProtocol

        self.wsio = wsio


class AlexServerProtocol(WebSocketServerProtocol):
    """Twisted Protocol that takes care of communication with the Alex client."""
    def onConnect(self, request):
        self.factory.wsio.on_client_connected(self, request)

    def onMessage(self, payload, isBinary):
        if isBinary:
            self.factory.wsio.on_client_message_received(payload)

    def onClose(self, wasClean, code, reason):
        self.factory.wsio.on_client_closed()


class AlexPingFactory(WebSocketClientFactory):
    """Twisted Factory that takes care of sending a ping request to the WSRouter to let him know we are alive."""
    def __init__(self, addr, port, wsio):
        super(AlexPingFactory, self).__init__("ws://%s:%d" % (addr, port), debug=True)
        self.protocol = AlexPingProtocol

        self.wsio = wsio


class AlexPingProtocol(WebSocketClientProtocol):
    """Protocol that takes care of sending a ping request to the WSRouter so that it knows we are alive."""
    ping_interval = 1  # Ping will be sent every second.

    def onOpen(self):
        # Ping forever.
        def ping():
            msg = self.factory.wsio.build_ping_message()
            self.sendMessage(msg.SerializeToString(), True)
            self.factory.reactor.callLater(self.ping_interval, ping)

        ping()

    def clientConnectionFailed(self, connector, reason):
        self.retry(connector)

    def clientConnectionLost(self, connector, reason):
        self.retry(connector)