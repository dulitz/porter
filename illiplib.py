"""
Interface module for Lutron Homeworks Illumination over telnet

Author:
  Daniel Dulitz (https://github.com/dulitz)
with overall organization inspired by
  upsert (https://github.com/upsert)

see http://www.lutron.com/TechnicalDocumentLibrary/HWI%20RS232%20Protocol.pdf

Note that Illumination only allows a small number of simultaneous telnet connections --
on the order of 1 or 2. Beyond that, it will close connections silently with no
error message. This shows up in our logs as many "Empty read from the bridge" messages
and fewer "connection opened" messages.
"""

import asyncio, logging, re
from enum import IntEnum

CONF_ID = "id"
CONF_NAME = "name"
CONF_TYPE = "type"
CONF_SCENE_ID = "scene_id"
CONF_AREA_NAME = "area_name"
CONF_BUTTONS = "buttons"

_LOGGER = logging.getLogger(__name__)

class IlluminationClient:
    """Communicate with a Lutron Homeworks Illumination controller."""

    READ_SIZE = 1024
    DEFAULT_USER = b"lutron"
    DEFAULT_PASSWORD = b"integration"
    DEFAULT_PROMPT = b"GNET> "
    # login successful [handled explicitly in open()]
    # Keypad button monitoring enabled [handled explicitly in open()]
    # Processor Time: 15:22
    # Processor Time: 15:22:33
    OOB_RESPONSE_RE = re.compile(b'Processor ([^\r])*\r\n')
    # KBP, [01:06:12],  5    [also KBR, KBH, KBDT, DBP, DBR, DBH, DBDT, SVBP, SVBR, SVBH, SVBDT]
    # DL, [01:04:02:06],   0.00
    # KLS, [01:06:12], 000011100000000000000000
    # SVS, [01:06:03], 1, MOVING
    # GSS, [01:04:03], 1
    RESPONSE_RE = re.compile(b'([A-Z]+), *\\[([0-9.:]+)\\], *([0-9.]+)(, *([0-9.]+))? *\r\n')

    # we can send RDL to request a dimmer level, FADEDIM to set one, FRPM, FV, GSS, SVSS to set scenes,
    # and CCOPULSE, CCOCLOSE, CCOOPEN to control contacts.

    class Action(IntEnum):
        """Action numbers for the OUTPUT command."""

        SET      = 1    # Get or Set Zone Level
        RAISING  = 2    # Start Raising
        LOWERING = 3    # Start Lowering
        STOP     = 4    # Stop Raising/Lowering

        PRESET   = 6    # SHADEGRP for Homeworks QS

    class Button(IntEnum):
        """Action numbers for the DEVICE command."""

        PRESS = 3
        RELEASE = 4
        HOLD = 5
        DOUBLETAP = 6

        LEDSTATE = 9    # "Button" is a misnomer; this queries LED state

    class State(IntEnum):
        """Connection state values."""

        Closed = 1
        Opening = 2
        Opened = 3

    def __init__(self):
        """Initialize the library."""
        self._read_buffer = b""
        self._read_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._state = IlluminationClient.State.Closed
        self._host = None
        self._port = 23
        self._username = IlluminationClient.DEFAULT_USER
        self._password = IlluminationClient.DEFAULT_PASSWORD
        self.prompt = IlluminationClient.DEFAULT_PROMPT
        self.reader, self.writer = None, None

    def is_connected(self) -> bool:
        """Return if the connection is open."""
        return self._state == IlluminationClient.State.Opened

    async def open(self, host, port=23, username=DEFAULT_USER,
                   password=DEFAULT_PASSWORD):
        """Open a telnet connection to the controller."""
        async with self._read_lock:
            async with self._write_lock:
                if self._state != IlluminationClient.State.Closed:
                    return
                self._state = IlluminationClient.State.Opening

                self._host = host
                self._port = port
                self._username = username
                self._password = password

                # open connection
                try:
                    connection = await asyncio.open_connection(host, port)
                except OSError as err:
                    _LOGGER.warning(f'error opening connection to Illumination {host}:{port}: {err}')
                    self._state = IlluminationClient.State.Closed
                    return

                self.reader = connection[0]
                self.writer = connection[1]

                def cleanup(err):
                    _LOGGER.warning(f'error opening connection to Illumination {host}:{port}: {err}')
                    self._state = IlluminationClient.State.Closed

                # do login
                if await self._read_until(b'LOGIN: ') is False:
                    return cleanup('no login prompt')
                self.writer.write(username + b',' + password + b'\r\n')
                await self.writer.drain()
                if await self._read_until(b'login successful\r\n') is False:
                    return cleanup('login failed')

                for mon in [b'kbmon\r\n', b'dlmon\r\n', b'klmon\r\n', b'gsmon\r\n']:
                    self.writer.write(mon) # turn on monitoring
                    await self.writer.drain()
                    if await self._read_until(b'monitoring enabled\r\n') is False:
                        return cleanup('set monitoring failed')

                _LOGGER.info(f'opened Homeworks Illumination connection {host}:{port}')
                self._state = IlluminationClient.State.Opened

    async def _read_until(self, value):
        """Read until a given value is reached."""
        while True:
            if hasattr(value, "search"):
                # detected regular expression
                match = value.search(self._read_buffer)
                if match:
                    self._read_buffer = self._read_buffer[match.end():]
                    return match
            else:
                assert type(value) == type(b''), value
                where = self._read_buffer.find(value)
                if where != -1:
                    until = self._read_buffer[:where+len(value)]
                    self._read_buffer = self._read_buffer[where + len(value):]
                    return until
            try:
                read_data = await self.reader.read(IlluminationClient.READ_SIZE)
                if not len(read_data):
                    _LOGGER.info('controller disconnected')
                    return False
                self._read_buffer += read_data
            except OSError as err:
                _LOGGER.warning(f'error reading from controller: {err}')
                return False

    RAWMAP = {
        'KBP':  ('DEVICE', Button.PRESS),
        'KBR':  ('DEVICE', Button.RELEASE),
        'KBH':  ('DEVICE', Button.HOLD),
        'KBDT': ('DEVICE', Button.DOUBLETAP),
        'DBP':  ('DEVICE', Button.PRESS),
        'DBR':  ('DEVICE', Button.RELEASE),
        'DBH':  ('DEVICE', Button.HOLD),
        'DBDT': ('DEVICE', Button.DOUBLETAP),
        'SVBP': ('DEVICE', Button.PRESS),
        'SVBR': ('DEVICE', Button.RELEASE),
        'SVBH': ('DEVICE', Button.HOLD),
        'SVBDT': ('DEVICE', Button.DOUBLETAP),
        }
    async def read(self):
        """maps the result of read_raw() to correspond to the result of read() on liplib"""
        a, b, c, d = await self.read_raw()
        if a is None:
            return a, b, c, d
        if a == 'DL' or a == 'GSS':
            return 'OUTPUT', b, IlluminationClient.Action.SET, c
        (newa, newd) = self.RAWMAP.get(a, (None, None))
        if newa is not None:
            if d is not None:
                _LOGGER.warning(f'unexpected final field in {a} {b} {c} {d} with {newa} {newd}')
            return newa, b, c, newd
        # we pass through these without change:
        #   KLS, [01:06:12], 000011100000000000000000
        #   SVS, [01:06:03], 1, MOVING
        return a, b, c, d
            
    async def read_raw(self):
        """Return a list of values read from the Telnet interface."""
        async with self._read_lock:
            if self._state != IlluminationClient.State.Opened:
                return None, None, None, None
            match = await self._read_until(IlluminationClient.RESPONSE_RE)
            if match is not False:
                # 1 = mode, 2 = integration number [address],
                # 3 = button number, 4 = value
                fourth = match.group(4).decode('ascii') if match.group(4) else None
                try:
                    address = int(match.group(2).decode('ascii').replace(':', ''))
                    return (match.group(1).decode('ascii'),
                            address, float(match.group(3)),
                            fourth)
                except ValueError:
                    _LOGGER.warning("cannot convert: ", match.group(0))
        if match is False:
            # attempt to reconnect
            _LOGGER.info(f'reconnecting to controller {self._host}')
            self._state = IlluminationClient.State.Closed
            await self.open(self._host, self._port, self._username,
                            self._password)
        return None, None, None, None

    async def write(self, mode, integration, action, *args, value=None):
        """Write a list of values to the telnet interface."""
        if hasattr(action, "value"):
            action = action.value
        async with self._write_lock:
            if self._state != IlluminationClient.State.Opened:
                return
            data = f'#{mode},{integration},{action}'
            if value is not None:
                data += f',{value}'
            for arg in args:
                if arg is not None:
                    data += f',{arg}'
            try:
                self.writer.write((data + "\r\n").encode("ascii"))
                await self.writer.drain()
            except OSError as err:
                _LOGGER.warning("Error writing to the controller: %s", err)

    async def query(self, mode, integration, action):
        """Query a device to get its current state."""
        if hasattr(action, "value"):
            action = action.value
        _LOGGER.debug("Sending query %s, integration %s, action %s",
                      mode, integration, action)
        async with self._write_lock:
            if self._state != IlluminationClient.State.Opened:
                return
            self.writer.write(f'rdl,{self.to_illumination_address(integration)}\r\n'.encode())
            await self.writer.drain()

    def to_illumination_address(self, integration):
        s = str(integration)
        if len(s) % 2 == 1:
            s = f'0{s}'
        return ':'.join([s[i:i+2] for i in range(0, len(s), 2)])

    async def ping(self):
        """Ping the interface to keep the connection alive."""
        async with self._write_lock:
            if self._state != IlluminationClient.State.Opened:
                return
            self.writer.write(b"rst\r\n")
            await self.writer.drain()

    async def logout(self):
        """Logout and sever the connection to the bridge."""
        async with self._write_lock:
            if self._state != IlluminationClient.State.Opened:
                return
            self.writer.write(b"quit\r\n")
            await self.writer.drain()
            self._state = IlluminationClient.State.Closed
