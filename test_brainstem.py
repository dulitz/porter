"""
test_brainstem.py

"""

import asyncio, logging
from datetime import datetime, date, timezone

# uncomment this to get more asyncio logs
# logging.basicConfig(level=logging.DEBUG)

import brainstem

class MockDatetime:
    def __init__(self):
        self.state = 0

    def now(self, tz):
        assert tz == timezone.utc
        self.state += 1
        if self.state == 1: # Timers.__init__()
            return datetime(2021, 7, 13, 15, 0, 0, tzinfo=timezone.utc)
        elif self.state == 2: # Brainstem.run()
            return datetime(2021, 7, 13, 15, 0, 0, tzinfo=timezone.utc)
        elif self.state == 3: # Timers.process_timers()
            return datetime(2021, 7, 13, 15, 0, 0, tzinfo=timezone.utc)
        elif self.state == 4: # Timers.process_timers()
            return datetime(2021, 7, 14, 3, 59, 59, tzinfo=timezone.utc)
        elif self.state == 5: # Timers.process_timers()
            return datetime(2021, 7, 14, 4, 0, 0, tzinfo=timezone.utc)
        elif self.state == 6: # Timers.process_timers()
            return datetime(2021, 7, 14, 4, 0, 0, tzinfo=timezone.utc)
        elif self.state == 7: # Timers.process_timers()
            return datetime(2021, 7, 14, 4, 0, 0, tzinfo=timezone.utc)
        elif self.state == 8: # Timers.process_timers()
            return datetime(2021, 7, 14, 4, 0, 1, tzinfo=timezone.utc)
        elif self.state == 9: # Timers.process_timers()
            return datetime(2021, 7, 14, 4, 0, 1, tzinfo=timezone.utc)
        else:
            assert False, self.state

    def today(self):
        return date(2021, 7, 14)
    def strptime(self, *args):
        return datetime.strptime(*args)
    def combine(self, *args, tzinfo=None):
        if tzinfo is None:
            return datetime.combine(*args)
        else:
            return datetime.combine(*args, tzinfo=tzinfo)

mockdatetime = MockDatetime()
brainstem.datetime = mockdatetime
brainstem.date = mockdatetime

class MockAsyncio:
    def __init__(self):
        self.state = 0

    async def sleep(self, secs, next_coro=None):
        if self.state == 0:
            assert secs == 32400, secs
        elif self.state == 1:
            assert secs == 1, secs
        else:
            assert False, self.state
        self.state += 1
        return next_coro

mockasyncio = MockAsyncio()
brainstem.asyncio = mockasyncio

config = {
    'brainstem': {
        'actions': {
            'pump_on': [['module', 'target', 'sel', 'on'], ['module', 'target2', 'sel', 'yeah']],
            'pump_off': [['module', 'target', 'sel', 'off', 'the', 'best', 'arg']]
        },
        'timers': [[40000, 'pump_off']],
        'reactions': [['module', 'target', [100, 'desc', 7], 'pump_on']]
    }
}

class MockModule:
    def __init__(self):
        self.state = 0

    async def run(self, target, selector, command, *args):
        if self.state == 0 or self.state == 1:
            assert target == 'target', target
            assert selector == 'sel', selector
            assert command == 'off', command
            assert args == ('the', 'best', 'arg')
        elif self.state == 2:
            assert target == 'target', target
            assert selector == 'sel', selector
            assert command == 'on', command
            assert not args, args
        elif self.state == 3:
            assert target == 'target2', target
            assert selector == 'sel', selector
            assert command == 'yeah', command
            assert not args, args
        else:
            assert False, f'bad state {self.state}'
        self.state += 1

mockmodule = MockModule()
bs = brainstem.Brainstem(config)
bs.register_modules({'module': mockmodule})

def assert_state(module_s, datetime_s, asyncio_s):
    assert module_s == mockmodule.state, f'mockmodule in state {mockmodule.state}, should be {module_s}'
    assert datetime_s == mockdatetime.state, f'mockdatetime in state {mockdatetime.state}, should be {datetime_s}'
    assert asyncio_s == mockasyncio.state, f'mockasyncio in state {mockasyncio.state}, should be {asyncio_s}'

assert_state(0, 1, 0) # datetime runs in Brainstem.__init__()
asyncio.run(bs.run('pump_off'), debug=True)

assert_state(1, 2, 0) # datetime runs in Brainsteam.run()
t1 = asyncio.run(bs.timers.process_timers(), debug=True) # t1 is MockAsyncio.sleep()

assert_state(1, 3, 0)
print('running t1')
t2 = asyncio.run(t1, debug=True) # t2 is Timers.process_timers()

assert_state(1, 3, 1)
print('running t2')
t3 = asyncio.run(t2, debug=True) # t3 is MockAsyncio.sleep()

assert_state(1, 4, 1)
print('running t3')
t4 = asyncio.run(t3, debug=True) # t4 is Timers.process_timers(), runs pump_off timer

assert_state(1, 4, 2)
print('running t4')
t5 = asyncio.run(t4, debug=True)

assert_state(2, 6, 2)
tt = bs.observe_event('othermodule', 'whatever', 'blah')
assert not tt, tt

assert_state(2, 7, 2)
tt = bs.observe_event('module', 'target', [100, 'blahblahbalh', 7])
tt2 = asyncio.run(tt, debug=True)

assert tt2 == None, tt2
assert_state(4, 9, 2)

print('success')
