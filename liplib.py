"""
Interface module for Lutron Integration Protocol (LIP) over Telnet.

This module connects to a Lutron hub through the Telnet interface which, for
Radio Ra2 Select, must be enabled through the integration menu in the Lutron mobile app.

Authors:
upsert (https://github.com/upsert)
Daniel Dulitz (https://github.com/dulitz)

Based on Casetify from jhanssen
https://github.com/jhanssen/home-assistant/tree/caseta-0.40

From https://github.com/upsert/liplib/blob/master/liplib/__init__.py
Last Modified March 28, 2020; current as of 9 May 2021.
Apache-2.0 license.
"""

import asyncio
import json
import logging
import re
from enum import IntEnum

CONF_ID = "id"
CONF_NAME = "name"
CONF_TYPE = "type"
CONF_SCENE_ID = "scene_id"
CONF_AREA_NAME = "area_name"
CONF_BUTTONS = "buttons"

_LOGGER = logging.getLogger(__name__)

def load_integration_report(integration_report) -> list:
    """Process a JSON integration report and return a list of devices.

    Each returned device will have an 'id', 'name', 'type' and optionally
    a list of button IDs under 'buttons' for remotes
    and an 'area_name' attribute if the device is assigned to an area.
    """
    devices = []
    lipidlist = integration_report.get('LIPIdList')
    assert lipidlist, integration_report

    # lights and switches are in Zones
    for zone in lipidlist.get('Zones', []):
        device_obj = {CONF_ID: zone['ID'],
                      CONF_NAME: zone['Name'],
                      CONF_TYPE: 'light'}
        name = zone.get('Area', {}).get('Name', '')
        if name:
            device_obj[CONF_AREA_NAME] = name
        devices.append(device_obj)

    # remotes are in Devices, except ID 1 which is the bridge itself
    for device in lipidlist.get('Devices', []):
        # extract scenes from integration ID 1 - the smart bridge
        if device['ID'] == 1:
            for button in device.get('Buttons', []):
                if not button["Name"].startswith("Button "):
                    _LOGGER.info("Found scene %d, %s", button["Number"], button["Name"])
                    devices.append({CONF_ID: device["ID"],
                                    CONF_NAME: button["Name"],
                                    CONF_SCENE_ID: button["Number"],
                                    CONF_TYPE: "scene"})
        else:
            device_obj = {CONF_ID: device["ID"],
                          CONF_NAME: device["Name"],
                          CONF_TYPE: "sensor",
                          CONF_BUTTONS: [b["Number"] for b in device.get("Buttons", [])]}
            name = device.get('Area', {}).get('Name', '')
            device_obj[CONF_AREA_NAME] = name
            devices.append(device_obj)

    return devices


# pylint: disable=too-many-instance-attributes
class LipServer:
    """Communicate with a Lutron bridge or other Lutron device."""

    READ_SIZE = 1024
    DEFAULT_USER = b"lutron"
    DEFAULT_PASSWORD = b"integration"
    DEFAULT_PROMPT = b"GNET> "
    RESPONSE_RE = re.compile(b"~([A-Z]+),([0-9.]+),([0-9.]+),([0-9.]+)\r\n")
    OUTPUT = "OUTPUT"
    DEVICE = "DEVICE"

    class Action(IntEnum):
        """Action values."""

        # Get or Set Zone Level
        SET = 1
        # Start Raising
        RAISING = 2
        # Start Lowering
        LOWERING = 3
        # Stop Raising/Lowering
        STOP = 4

    class Button(IntEnum):
        """Button values."""

        PRESS = 3
        RELEASE = 4

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
        self._state = LipServer.State.Closed
        self._host = None
        self._port = 23
        self._username = LipServer.DEFAULT_USER
        self._password = LipServer.DEFAULT_PASSWORD
        self.prompt = LipServer.DEFAULT_PROMPT
        self.reader, self.writer = None, None

    def is_connected(self) -> bool:
        """Return if the connection is open."""
        return self._state == LipServer.State.Opened

    async def open(self, host, port=23, username=DEFAULT_USER,
                   password=DEFAULT_PASSWORD):
        """Open a Telnet connection to the bridge."""
        async with self._read_lock:
            async with self._write_lock:
                if self._state != LipServer.State.Closed:
                    return
                self._state = LipServer.State.Opening

                self._host = host
                self._port = port
                self._username = username
                self._password = password

                # open connection
                try:
                    connection = await asyncio.open_connection(host, port)
                except OSError as err:
                    _LOGGER.warning("Error opening connection"
                                    " to the bridge: %s", err)
                    self._state = LipServer.State.Closed
                    return

                self.reader = connection[0]
                self.writer = connection[1]

                # do login
                await self._read_until(b"login: ")
                self.writer.write(username + b"\r\n")
                await self.writer.drain()
                await self._read_until(b"password: ")
                self.writer.write(password + b"\r\n")
                await self.writer.drain()
                await self._read_until(self.prompt)

                self._state = LipServer.State.Opened

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
                read_data = await self.reader.read(LipServer.READ_SIZE)
                if not len(read_data):
                    _LOGGER.warning("Empty read from the bridge (clean disconnect)")
                    return False
                self._read_buffer += read_data
            except OSError as err:
                _LOGGER.warning("Error reading from the bridge: %s", err)
                return False

    async def read(self):
        """Return a list of values read from the Telnet interface."""
        async with self._read_lock:
            if self._state != LipServer.State.Opened:
                return None, None, None, None
            match = await self._read_until(LipServer.RESPONSE_RE)
            if match is not False:
                # 1 = mode, 2 = integration number,
                # 3 = action number, 4 = value
                try:
                    return match.group(1).decode("ascii"), \
                           int(match.group(2)), int(match.group(3)), \
                           float(match.group(4))
                except ValueError:
                    print("Exception in ", match.group(0))
        if match is False:
            # attempt to reconnect
            _LOGGER.info("Reconnecting to the bridge %s", self._host)
            self._state = LipServer.State.Closed
            await self.open(self._host, self._port, self._username,
                            self._password)
        return None, None, None, None

    async def write(self, mode, integration, action, *args, value=None):
        """Write a list of values out to the Telnet interface."""
        if hasattr(action, "value"):
            action = action.value
        async with self._write_lock:
            if self._state != LipServer.State.Opened:
                return
            data = "#{},{},{}".format(mode, integration, action)
            if value is not None:
                data += ",{}".format(value)
            for arg in args:
                if arg is not None:
                    data += ",{}".format(arg)
            try:
                self.writer.write((data + "\r\n").encode("ascii"))
                await self.writer.drain()
            except OSError as err:
                _LOGGER.warning("Error writing out to the bridge: %s", err)


    async def query(self, mode, integration, action):
        """Query a device to get its current state."""
        if hasattr(action, "value"):
            action = action.value
        _LOGGER.debug("Sending query %s, integration %s, action %s",
                      mode, integration, action)
        async with self._write_lock:
            if self._state != LipServer.State.Opened:
                return
            self.writer.write("?{},{},{}\r\n".format(mode, integration,
                                                     action).encode())
            await self.writer.drain()

    async def ping(self):
        """Ping the interface to keep the connection alive."""
        async with self._write_lock:
            if self._state != LipServer.State.Opened:
                return
            self.writer.write(b"#PING\r\n")
            await self.writer.drain()

    async def logout(self):
        """Logout and sever the connect to the bridge."""
        async with self._write_lock:
            if self._state != LipServer.State.Opened:
                return
            self.writer.write(b"LOGOUT\r\n")
            await self.writer.drain()
            self._state = LipServer.State.Closed
