"""
brainstem.py

Reactions happen here.

  When Lutron values change, perform an action.
     - Any sleep button turns off Laundry Room lights.
     - Master Bathroom and Guest Bathroom occupancy sensors turn on Rinnai
       recirculation for 5 min.

"""

import asyncio, heapq, logging, prometheus_client, requests
from datetime import time, datetime, timedelta, timezone


LOGGER = logging.getLogger('porter.brainstem')


class BrainstemError(Exception):
    pass


class Timers:
    def __init__(self, timers, runner):
        self.runner = runner
        zero = datetime.strptime('000000', '%H%M%S')
        def cnv(hhmmss):
            try:
                return datetime.strptime('%06d' % hhmmss, '%H%M%S').time().replace(tzinfo=timezone.utc)
            except ValueError:
                LOGGER.error(f'could not parse brainstem timer {hhmmss} in {timers}')
                raise
        self.timers = [(cnv(hhmmss), action) for (hhmmss, action) in timers]
        now = datetime.now(timezone.utc)
        self.today = now.date()
        self.heap_of_timers = [(datetime.combine(self.today, t), action)
                               for (t, action) in self.timers
                               if datetime.combine(self.today, t) >= now]
        heapq.heapify(self.heap_of_timers)

    async def run(self, action):
        return await self.runner(action)

    async def process_timers(self):
        """Waits for the next timer and runs it. Returns a coroutine to run next."""
        now = datetime.now(timezone.utc)
        if self.today < now.date():
            self.today = now.date()
            while self.heap_of_timers:
                (t, action) = heapq.heappop(self.heap_of_timers)
                await self.run(action)
            self.heap_of_timers = [(datetime.combine(self.today, t), action)
                                   for (t, action) in self.timers]
            heapq.heapify(self.heap_of_timers)
        if self.heap_of_timers:
            # then run the next timer if it's runnable; otherwise wait until runnable
            future = (self.heap_of_timers[0][0] - now).total_seconds()
            if future <= 0:
                (t, action) = heapq.heappop(self.heap_of_timers)
                LOGGER.info(f'timer running {action} scheduled {-future}s ago')
                try:
                    await self.run(action)
                except Exception as ex:
                    LOGGER.error(f'timer running {action}', exc_info=ex)
            else:
                return asyncio.sleep(future, self.process_timers())
        else:
            midnight = datetime.combine(self.today + timedelta(days=1), time(), tzinfo=timezone.utc)
            return asyncio.sleep((midnight - now).total_seconds(), self.process_timers())
        return self.process_timers()


class EventPropagator:
    def __init__(self, bclient, modulename, target=None):
        self.bclient = bclient
        self.modulename = modulename
        self.targ = target
        self.awaitables = set()
        self.children = []
    def target(self, targ):
        assert self.targ is None
        ep = EventPropagator(self.bclient, self.modulename, targ)
        self.children.append(ep)
        return ep
    def propagate(self, selector):
        assert self.targ
        try:
            coro = self.bclient.observe_event(self.modulename, self.targ, selector)
            if coro:
                self.awaitables.add(coro)
        except Exception as ex:
            LOGGER.error(f'exception in propagate() {self.modulename} {self.targ} {selector}', exc_info=ex)
    def add_awaitables_to(self, otherset):
        for child in self.children:
            child.add_awaitables_to(otherset)
        otherset |= self.awaitables
        self.awaitables = set()


class CircularBuffer:
    # TODO: could be more efficient
    def __init__(self, size):
        self.size = size
        self.items = []
    def add(self, item):
        self.items.append(item)
        if len(self.items) <= self.size:
            return False
        del self.items[0]
        return True


class Brainstem:
    def __init__(self, config):
        self.config = config
        self.module_to_client = {}
        myconfig = self.config['brainstem']
        self.timers = Timers(myconfig.get('timers', []), self.run)
        self.reactions = {}
        self.ratelimits = {}
        self.eventbuffer = CircularBuffer(10)
        for (mod, target, selector, cmd) in myconfig.get('reactions', []):
            m = self.reactions.get(mod)
            if not m:
                m = {}
                self.reactions[mod] = m
            t = m.get(target)
            if not t:
                t = {}
                m[target] = t
            tup = tuple(selector)
            assert tup not in t
            t[tup] = cmd

    def register_modules(self, module_to_client):
        self.module_to_client = module_to_client

    def module(self, modulename):
        # self.module('foo').target('bar').propagate(selector) will call observe_event()
        return EventPropagator(self, modulename)

    async def run(self, action, next_coro=None):
        """
        Coroutine to execute "action," which must be the name of a sequence
        in brainstem.actions. The actions within that sequence are executed
        in order. After the last step has been executed and our task is complete,
        we return next_coro so it is scheduled to replace our task.
        """
        self.eventbuffer.add((datetime.now(timezone.utc), 'run', action))
        seq = self.config['brainstem'].get('actions', {}).get(action, [])
        assert seq, action # FIXME: verify this at load time
        for a in seq:
            if isinstance(a, str):
                await self.run(a)
            elif len(a) < 4:
                (funcname, *args) = a
                if funcname == 'ratelimit' and len(args) == 1:
                    last = self.ratelimits.get(action, 0)
                    now = time.time()
                    if now - last < float(args[0]):
                        LOGGER.debug(f'action {action} inhibited by ratelimit')
                        return next_coro
                    self.ratelimits[action] = now
            else:
                (module, target, selector, command, *args) = a
                client = self.module_to_client[module] # FIXME: verify this at load time
                await client.run(target, selector, command, *args)
        return next_coro

    def observe_event(self, module, target, selector):
        """Module knows that the event described by selector has occurred on target.
        We are ultimately called. If we return None, there is no reaction to the event.
        Otherwise we return a coroutine that, when scheduled, triggers the reaction.
        """
        LOGGER.debug(f'observe_event {module} {target} {selector}')
        self.eventbuffer.add((datetime.now(timezone.utc), 'observed', module, target, selector))
        for (sel, cmd) in self.reactions.get(module, {}).get(target, {}).items():
            if (sel[0] == selector[0] or sel[1] in selector[1]) and sel[2:] == selector[2:]:
                LOGGER.debug(f'scheduling {cmd} to execute')
                return self.run(cmd)
        return None

    def collect(self, target):
        """Don't actually probe this from Prometheus; all the data is in the descriptions."""
        from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily
        gmfs = []
        nnn = 0
        for (dt, direction, name, *args) in self.eventbuffer.items:
            desc = f'{dt.ctime()}: {direction} {name} {args}'
            gmf = GaugeMetricFamily(f'event{nnn}', desc, labels=[])
            nnn += 1
            gmf.add_metric([], 1 if direction == 'run' else 0)
            gmfs.append(gmf)
        return gmfs

    async def poll(self):
        await asyncio.sleep(60)
        return self.poll()

    def get_awaitables(self):
        # self.poll() doesn't do anything yet so we don't return it
        return set([self.timers.process_timers()])
