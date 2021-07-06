"""
test_lutron.py

Currently we only test the mutation operations: run() etc.

TODO: Test the read operations too.
"""

import asyncio
import lutron, liplib

config = {
    'lutron': {
        'illumtarget': {
            'system': 'Illumination',
            'user': 'mine',
            'password': 'yes',
            'areas': {
                'Entry': [[10615, 'Front Door Keypad', [1, 3, 5, 7, 19]], [101000301, 'Pendant'], [101010101, 'Downlights'], [101010102, 'Bridge Downlights']]
            }
        },
        'qstarg': {
            'user': 'mine',
            'password': 'yes',
            'areas': {
                'Kitchen': [[166, 'Radiant Heat'], [177, 'Espresso']]
            }
        }
    }
}

class MockReader:
    def __init__(self):
        pass
    def read(self, size):
        return b'blah '

class MockWriter:
    def __init__(self, match):
        self.match = match
        self.state = 0
    def write(self, b):
        if self.state == 0:
            assert b == self.match, b
        else:
            assert False, self.state
        self.state += 1
            
    async def drain(self):
        assert self.state == 1, self.state
        self.state += 1

def lipsetup(config, targ, client):
    params = lutron.ConfigParams(config['lutron'], targ)
    lips = lutron.Lipservice(targ, 23, params)
    lips.lipserver._state = liplib.LipServer.State.Opened
    client.manager.target_to_cfparams[targ] = params
    client.manager.hostport_to_lipservice[(targ, 23)] = lips
    lips.lipserver.reader = MockReader()
    return lips

client = lutron.LutronClient(config)
illips = lipsetup(config, 'illumtarget', client)
illips.lipserver.writer = MockWriter(b'FADEDIM,50,0,0,01:01:00:03:01\r\n')

lips = lipsetup(config, 'qstarg', client)
lips.lipserver.writer = MockWriter(b'#OUTPUT,177,1,0\r\n')

asyncio.run(client.run('illumtarget', 'Pendant', 'setlevel', 50))
assert illips.lipserver.writer.state == 2, illips.lipserver.writer.state
assert lips.lipserver.writer.state == 0, lips.lipserver.writer.state

asyncio.run(client.run('qstarg', 177, 'setlevel', 0))
assert lips.lipserver.writer.state == 2, lips.lipserver.writer.state
assert illips.lipserver.writer.state == 2, illips.lipserver.writer.state

print('success')
