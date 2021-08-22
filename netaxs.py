"""
netaxs.py

The Honeywell NetAXS-123 module for porter, the Prometheus exporter.

Tested with
   firmware version 6.0.10.5
   OS       version 2.6.25#107 Tue Jan 10 10:55:47 CST 2012

Since this isn't a documented API and I just hacked it from looking at the
internals of the webapp, it should be expected to break whenever you
update the device firmware.
"""

import asyncio, json, logging, time, threading, ssl
import requests, prometheus_client, pytz, websockets

from datetime import datetime
from enum import Enum
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

REQUEST_TIME = prometheus_client.Summary('netaxs_processing_seconds',
                                         'time of netaxs requests')
LOGIN_ATTEMPTS = prometheus_client.Gauge('netaxs_login_attempts',
                                         'how many times we have logged in to netaxs',
                                         ['uri'])
LOGGER = logging.getLogger('porter.netaxs')

class NetaxsError(Exception):
    pass


class Session:
    class _Request(Enum):
        """When sync code wants async code to close or (re)open the websocket,
        self.async_request is set to CLOSE or OPEN.
        """
        NONE  = 0
        OPEN  = 1
        CLOSE = 2

    def __init__(self, uri, user, password, timeout, verify=None, timezone=''):
        """The password must have already been hashed according to the NetAXS algorithm."""
        self.uri, self.user, self.password = uri, user, password
        self.timeout, self.verify = timeout, verify
        self.timezone = pytz.timezone(timezone) if timezone else None
        self.session = None
        if self.uri.find('://') == -1:
            self.uri = f'https://{self.uri}'
        while self.uri.endswith('/'):
            self.uri = self.uri[:len(self.uri)-1]
        self.cv = threading.Condition()
        self.websocket = None
        self.async_request = Session._Request.NONE
        self.failed_fetches = 0

    def open(self):
        if self.session:
            return
        LOGGER.info(f'opening connection to {self.uri}')
        LOGIN_ATTEMPTS.labels(uri=self.uri).inc()
        self.failed_fetches = 0
        self.session = requests.Session()
        self.session.verify = self.verify

        authinfo = { 'user': self.user, 'pwd': self.password }
        p = self.session.post(f'{self.uri}/lib/login.lsp', data=authinfo, timeout=self.timeout)
        p.raise_for_status()
        self._debug('duringlogin', p)
        r = p.json()
        statuscode = r['statuscode']
        if statuscode == '1':
            raise NetaxsError(f'username {r["username"]} not found')
        elif statuscode == '2':
            raise NetaxsError(f'incorrect password for username {r["username"]}')
        elif statuscode == '3':
            raise NetaxsError(f'expired password for username {r["username"]}')
        elif statuscode == '4':
            raise NetaxsError(f'retry limit exceeded for username {r["username"]}')
        elif statuscode == '5':
            raise NetaxsError(f'system error for username {r["username"]}')
        elif statuscode == '6':
            raise NetaxsError(f'username {r["username"]} is locked out')
        elif statuscode == '7':
            raise NetaxsError(f'username {r["username"]} is disabled')

        # the next post sets the actual signed-in cookies and/or sets the
        # application state to "signed in"
        pp = self.session.post(f'{self.uri}/views/home/index.lsp', data={ 'ba_username': self.user, 'ba_password': self.password }, timeout=self.timeout)
        pp.raise_for_status()
        self._debug('afterlogin', pp)
        self.async_request = Session._Request.OPEN  # open or re-open as needed
  
    def close(self):
        LOGGER.info(f'closing connection to {self.uri}')
        self.session.close()
        self.session = None
        self.async_request = Session._Request.CLOSE

    async def async_close(self):
        LOGGER.info(f'closing websocket for {self.uri}')
        await self.websocket.close()
        self.websocket = None
        # FIXME: commented this out to avoid race condition
        # self.async_request = Session._Request.NONE

    async def async_open(self):
        assert self.session  # must be signed in
        self.readlock, self.writelock = asyncio.Lock(), asyncio.Lock()
        if not self.websocket:
            LOGGER.info(f'opening websocket for {self.uri}')
            sslcontext = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            sslcontext.load_verify_locations(self.verify)
            self.wssuri = f'{self.uri}/views/EventHandlerIntf/'.replace('https', 'wss', 1)
            cookies = '; '.join([f'{k}={v}' for (k, v) in self.session.cookies.items()])
            extra_headers = { 'Cookie': cookies }
            self.websocket = await websockets.connect(
                self.wssuri, ssl=sslcontext, origin=self.uri, max_size=4096, max_queue=1024,
                ping_interval=None, ping_timeout=None, extra_headers=extra_headers)
            # FIXME: race condition here, where close() and open() may be called
            # on the session while we are still connecting in the line above, but
            # then we overwrite the new OPEN request in the line below
            self.async_request = Session._Request.NONE

    async def handle_async_request(self):
        assert self.websocket  # must have called async_open()
        if self.async_request == Session._Request.OPEN:
            if self.websocket:
                await self.async_close()
            await self.async_open()
        elif self.async_request == Session._Request.CLOSE:
            if self.websocket:
                await self.async_close()

    async def write_message(self, message):
        assert self.websocket  # must call async_open() first
        await self.handle_async_request()
        LOGGER.debug(f'sending {message} on {self.wssuri}')
        async with self.writelock:
            return await self.websocket.send(message)

    async def read_event(self):
        assert self.websocket  # must have called async_open()
        await self.handle_async_request()
        async with self.readlock:
            async for message in self.websocket:
                js = json.loads(message.replace("'", '"'))
                if len(js) == 2:
                    if js[0] == 'setCid':
                        cid = int(js[1])
                        LOGGER.debug(f'onConnect.cid={cid}')
                        await self.write_message('luaNS4Client2ServerIntf;setIOState;dddd;1;1;0;1;;')
                        continue
                elif len(js) == 4:
                    if js[0] == 'ud' and js[1] == 'luaNS4Server2ClientIntf':
                        if js[2] == 'asyncLogoff' and len(js[3]) == 1:
                            logoff_minutes = js[3][0]
                            return { js[2]: logoff_minutes }
                        elif js[2] == 'asyncSetIOState':
                            # js[3] [1,26,1,1,0]
                            return { js[2]: js[3] }
                        elif js[2] == 'asyncSendNewEvent':
                            (panel, datestr, evid, device, zero, logical, physical, typeint, code, site, lastname, secondzero, last) = js[3]
                            ts = self._localize(datetime.strptime(datestr.replace('\\/', '/').strip(), '%m/%d/%Y %H:%M:%S')).timestamp()
                            desc = ''
                            if typeint == 1: # and subtypeint == 0
                                if int(code) and site:
                                    desc = 'Card Found'
                            elif typeint == 2: # and subtypeint == 1:
                                if site == 0:
                                    desc = 'Card Not Found'
                            elif typeint == 5:
                                desc = 'Timezone Violation'
                            elif typeint == 12: # and subtypeint == 0:
                                desc = 'VIP Card Found'
                            elif typeint == 11: # and subtypeint == 1:
                                desc = 'Card Not Found: expired'
                            if not desc:
                                LOGGER.error(f'unknown event {js[3]}')
                                desc = 'unknown event'
                            return { js[2]: {
                                'id': int(evid),
                                'panel': panel,
                                'when': ts,
                                'reader': device,
                                'logical': int(logical),
                                'physical': int(physical),
                                'description': desc,
                                'code': code,
                                'site': site,
                                'name': lastname,
                            } }
                LOGGER.warning(f'unknown message from {self.wssuri}: {js}')

    def get_events(self, panel=1, start=0, notbefore=0):
        self._set_headers()
        assert panel == 1, f'other panels not supported {panel}'
        postdata = {
            'filter': """{"t":2,"a":[],"b":[],"c":"0","d":"","e":[],"f":[],"l":200,"o":%d,"s":0}""" % start
        }
        events = self.session.post(f'{self.uri}/models/events/getEvents.lsp', data=postdata, timeout=self.timeout)
        events.raise_for_status()
        self._debug('events', events)

        out = []
        js = events.json()
        # get assigned doors: [1,1,1,"Door1.1",1]
        # door I/O mapping: [1,1,0,1,1,2,3,4,2,1,5]
        # 1," 7\/19\/2021 09:32:49 ",4531,"Input 20: PANEL TAMPER",0,20,0,2,"0",0,"",        0,1
        # pnl,evid,evtype,evsubtype, space, logical, physical, zero, device, code, lastname, timedict, secondzero, pin/site
        # pin/site is 0 for error

        for (panel, evid, evtype, evsubtype, space, logical, physical, zero, device, code, lastname, timedict, secondzero, pin) in zip(*([iter(js)]*14)):
            try:
                typeint, subtypeint = int(evtype), int(evsubtype)
            except ValueError:
                typeint, subtypeint = -1, -1
            if typeint == 1:
                if subtypeint == 0:
                    if int(code) == 0:
                        desc = 'online'
                    else:
                        desc = 'Card Found'
                elif subtypeint == 2:
                    desc = 'common database update'
                elif subtypeint == 3:
                    desc = 'panel database update [post-upgrade]'
                else:
                    desc = f'unknown type 1 [good, found, success] subtype {evsubtype}'
            elif typeint == 2:
                if subtypeint == 0:
                    desc = 'EVL controller offline'
                elif subtypeint == 1:
                    if int(space) == 1:
                        desc = 'TAMPER'
                    else:
                        desc = 'Card Not Found'
                elif subtypeint == 2:
                    desc = 'panel database update [type 2]'
                else:
                    desc = f'unknown type 2 [offline, not found, bad] subtype {evsubtype}'
            elif typeint == 12 and subtypeint == 0:
                desc = 'VIP Card Found'
            elif typeint == 11 and subtypeint == 1:
                desc = 'Card Not Found: expired'
            elif typeint == 142 and subtypeint == 0:
                desc = 'firmware update in progress'
            elif typeint == 130 and subtypeint == 1:
                desc = 'panel restarted' # firmware revision in reader field
            elif typeint == 5 and subtypeint == 1:
                desc = 'timezone violation'
            else:
                desc = f'unknown type {evtype} subtype {evsubtype}'
            ts = self._localize(datetime(year=timedict['year'], month=timedict['month'], day=timedict['day'], hour=timedict['hour'], minute=timedict['min'], second=timedict['sec'])).timestamp()
            if ts < notbefore:
                break
            out.append({
                'id': int(evid),
                'when': ts,
                'panel': panel,
                'reader': device,
                'logical': int(logical),
                'physical': int(physical),
                'description': desc,
                'code': code,
                'name': lastname,
            })
        return out

    def get_web_events(self, panel=1, start=0, notbefore=0):
        self._set_headers()
        assert panel == 1, f'other panels not supported {panel}'
        data = { 'filter':
                 """{"t":4,"a":[],"b":[],"c":0,"d":"","e":[],"f":[],"l":0,"o":%d,"s":0}""" % start
        }
        p = self.session.post(f'{self.uri}/models/events/getEvents.lsp', data=data, timeout=self.timeout)
        self._debug('prewebevents', p)
        p.raise_for_status()
        r = p.json()
        statuscode = r[0]
        if int(statuscode) != 0:
            raise NetaxsError(f'get_web_events got error status {r}')

        self._set_headers()
        events = self.session.get(f'{self.uri}/models/WebEvents.csv', timeout=self.timeout)
        self._debug('webevents', events)
        events.raise_for_status()

        out = []
        for line in events.text.split('\n')[1:]: # first line is a header
            if not line:
                continue # ignore empty lines, such as at EOF
            values = line.strip().split(',') # CSV
            if len(values) == 3:
                (when, event_type, desc) = values
                notes = ''
            else:
                (when, event_type, desc, notes) = values
            ts = self._localize(datetime.strptime(
                when.strip(), '%m/%d/%Y %H:%M:%S')).timestamp()
            if ts < notbefore:
                break
            out.append({
                'notes': notes,
                'type': event_type,
                'description': desc,
                'when': ts,
            })
        return out

    def get_cards(self):
        self._set_headers()
        data = {
            'panelnum': 1,
            'type': 1,
            'subtype': 6,
            'oper': 0,
            'password': '',
        }
        p = self.session.post(f'{self.uri}/models/where/upload/processFile.lsp', data=data, timeout=self.timeout)
        p.raise_for_status()
        self._debug('precards', p)
        r = p.json()
        statuscode = r['status']
        if int(statuscode) == 9:
            pass
        elif int(statuscode) != 0 or r['failedPanels']:
            raise NetaxsError(f'error status during get_cards phase 1: {r}')

        self._set_headers()
        cards = self.session.get(f'{self.uri}/models/CardReport.csv', timeout=self.timeout)
        if cards.status_code == 404:
            self.failed_fetches += 1
            if self.failed_fetches > 3:
                LOGGER.error(f'{self.failed_fetches} consecutive failed fetches; reconnecting')
                self.close()
                self.open()
        cards.raise_for_status()
        self.failed_fetches = 0
        self._debug('cards', cards)

        out = []
        for line in cards.text.split('\n')[1:]: # first line is a header
            if not line:
                continue # ignore empty lines, such as at EOF
            (card, lastname, firstname, trace_enabled, card_type, uses_remaining, expiration_date, access_levels, site_code, pin, info1, info2, timezones, activation_date, issue_level, apb_state, control_device, access_group, last_swiped_time, remainder) = line.strip().split(',') # CSV
            d = {
                'card': int(card),
                'lastname': lastname,
                'firstname': firstname,
                'pin': pin, # not coverted to int since this is often empty
                'note1': info2,
                'note2': info1, # this is backwards, but info1 seems always empty in V6
                'type': card_type,
                'access': access_group.strip(';'),
                'activation': self._localize(datetime.strptime(
                    activation_date, '%m/%d/%Y')).timestamp(),
            }
            if uses_remaining:
                d['uses_remaining'] = int(uses_remaining)
            if expiration_date:
                d['expiration'] = self._localize(datetime.strptime(
                    expiration_date, '%m/%d/%Y')).timestamp()
            if last_swiped_time:
                d['last_swiped'] = self._localize(datetime.strptime(
                    last_swiped_time, '%m/%d/%Y %H:%M:%S')).timestamp()
            out.append(d)
        return out

    def get_badges(self):
        """In V6, operator does not have permission to do this."""
        self._set_headers()
        badges = self.session.post(f'{self.uri}/models/who/badge/getbadges.lsp', timeout=self.timeout)
        badges.raise_for_status()
        self._debug('badges', badges)

        out = []
        for (card, pin, note, use_limited, uses_remaining, card_type, has_expiration, expiresMonth, expiresDay, expiresYear, firstname, lastname, trace_enabled, activatedMonth, activatedDay, activatedYear, is_expired) in zip(*([iter(badges.json())]*17)):
            d = {
                'card': int(card),
                'lastname': lastname,
                'firstname': firstname,
                'pin': pin, # not coverted to int since this is often empty
                'note1': note,
                'activation': self._localize(datetime(year=int(activatedYear), month=int(activatedMonth), day=int(activatedDay))).timestamp(),
                'access': '' if is_expired else 'not expired',
            }
            if use_limited:
                d['uses_remaining'] = int(uses_remaining)
            if card_type == 2:
                d['type'] = 'employee'
            elif card_type == 1:
                d['type'] = 'VIP'
            elif card_type == 0:
                d['type'] = 'supervisor'
            else:
                d['type'] = f'unknown card type {card_type}'
            if has_expiration:
                d['expiration'] = self._localize(datetime(year=int(expiresYear), month=int(expiresMonth), day=int(expiresDay))).timestamp()
            out.append(d)
        return out
    
    def _set_headers(self):
        self.session.headers.update({
            'Referer': f'{self.uri}/views/home/index.lsp',
            'X-XSRF-TOKEN': self.session.cookies['XSRF-TOKEN']
        })

    def _localize(self, dtime):
        """Accepts a naive datetime dtime, in the timezone of self.timezone, and returns
        an aware datetime in that same timezone. If self.timezone is None, just returns
        dtime unchanged. Raises pytz.exceptions.AmbiguousTimeError if dtime is
        ambiguous (during a Daylight Savings transition window)."""
        return self.timezone.localize(dtime) if self.timezone else dtime

    def _debug(self, where, response):
        """during debugging, this method writes request/response info"""
        return # not debugging now :)
        with open(f'DEBUG-{basename}.html', 'w') as f:
            f.write(r.text)
        with open(f'DEBUG-{basename}.requests', 'w') as f:
            for h in r.history:
                f.write(f'request {h.url} {h.request.headers}\nresponse {h.status_code} {h.headers}\n\n')
            f.write(f'request {r.url} {r.request.headers}\nresponse {r.status_code} {r.headers}\n\n')
  

class NetaxsClient:
    def __init__(self, config, eventbus):
        self.config = config
        self.eventbus = eventbus
        self.cv = threading.Condition()
        self.targetmap = {}
        self.targeteventbusmap = {}
        self.awaitables = set()
        self.starttime = time.time()
        # Prometheus counters only show an increase after the first value, so
        # we want to get a zero into the system as soon as we can
        self.known_lnpns = ['1/1', '2/2', '3/3']  # maybe expand these?

        myconfig = config.get('netaxs')
        if not myconfig:
            raise Exception('no netaxs configuration')
        if not myconfig.get('timeout'):
            myconfig['timeout'] = 20
        if not myconfig.get('card_refetch_interval'):
            myconfig['card_refetch_interval'] = 0

    def _increment(self, d, key, increment=1):
        newv = d.get(key, 0) + increment
        d[key] = newv
        return newv

    def _get_session(self, target):
        """must hold self.cv upon call"""
        if target == 'verify' or target == 'verifysearch' or target == 'timeout':
            raise NetaxsError(f'netaxs target {target} is reserved and cannot be configured')
        s = self.targetmap.get(target)
        if s is None:
            targetconfig = self.config['netaxs'].get(target)
            if targetconfig is None:
                raise NetaxsError(f'no netaxs configuration for target {target}')
            user = targetconfig.get('user')
            if not user:
                raise NetaxsError(f'no netaxs user for target {target}')
            password = targetconfig.get('password')
            if not password:
                raise NetaxsError(f'no netaxs password for target {target}')
            timeout = targetconfig.get('timeout', self.config['netaxs']['timeout'])
            verify = targetconfig.get('verify', self.config['netaxs'].get('verify'))
            verifysearch = targetconfig.get('verifysearch', self.config['netaxs'].get('verifysearch'))
            if verify and verifysearch:
                if verify[0] == '/' or verify[0] == '.':
                    raise NetaxsError('specify verifysearch only when verify is a relative path not beginning with .')
                for prefix in verifysearch:
                    fullname = f'{prefix}{verify}'
                    try:
                        with open(fullname, 'r') as f:
                            f.read()
                            verify = fullname
                            break
                    except FileNotFoundError:
                        pass
                else:
                    LOGGER.warning(f'cannot find any verify file {verify} in search path {verifysearch}')
            timezone = targetconfig.get('timezone', '')
            s = Session(target, user, password, timeout, verify=verify, timezone=timezone)
            s.open()
            s.last_porter = {
                'adminlogins': 0, 'invalidpasswords': 0, 'dbupdates': {},
                'cardnotfound': {}, 'timezone': {}, 'cardfound': {}, 'card_timestamp': 0,
                'unknowneventtypes': 0, 'tamper': 0,
                'eventid': 0, 'timestamp': self.starttime - 5,
                'successful_io_timestamp': 0,
            }
            s.last_porter['dbupdates']['0/0'] = 0
            for vip in [True, False]:
                s.last_porter['cardfound'][vip] = {}
                for lnpn in self.known_lnpns:
                    s.last_porter['cardfound'][vip][lnpn] = 0
                    s.last_porter['cardnotfound'][lnpn] = 0
                    s.last_porter['timezone'][lnpn] = 0
            self.awaitables.add(self._coro_for_session(target, s, must_open=True))
            self.targetmap[target] = s
            self.targeteventbusmap[s.uri] = self.eventbus.target(target)
        return s

    def _retry_if_needed(self, session, func, tries=1):
        while True:
            try:
                return func()
            except (json.decoder.JSONDecodeError,
                    requests.exceptions.ChunkedEncodingError):
                LOGGER.info(f'closing session {session.uri} due to I/O error {func}')
                session.close()
                session.open()
                tries -= 1
                if tries == 0:
                    raise

    async def _coro_for_session(self, target, session, must_open=False):
        """Awaits asynchronous messages for session. When one arrives it is processed.
        If must_open is true, it calls async_open() first.
        """
        try:
            if must_open:
                await session.async_open()
            try:
                ev = await session.read_event()
            except websockets.exceptions.ConnectionClosedError:
                session.websocket = None
                await session.async_open()
                ev = await session.read_event()
            last = session.last_porter
            last['successful_io_timestamp'] = max(time.time(), last['successful_io_timestamp'])
            logoff_minutes = ev.get('asyncLogoff')
            if logoff_minutes is not None:
                LOGGER.debug(f'{session.uri}: asyncLogoff in {logoff_minutes} min')
                if logoff_minutes < 2:
                    with session.cv:
                        LOGGER.debug(f'{session.uri}: updating cards for keepalive')
                        self._update_cards(session, time.time())
            newevent = ev.get('asyncSendNewEvent')
            if newevent:
                LOGGER.debug(f'{session.uri}: new async event {newevent}')
                with self.cv:
                    self._update_one_event(session, newevent)
        except Exception as ex:
            LOGGER.error(f'{session.uri}: error reading websocket', exc_info=ex)
            await asyncio.sleep(1) # rate limiting
            # FIXME: if this happens repeatedly we should try to reconnect
        # schedule ourselves to run again
        return self._coro_for_session(target, session)

    async def poll(self):
        """Awaits events from each active Session. When one comes in, we update
        in advance of our next Prometheus poll.
        """
        awaiting = set()
        while True:
            with self.cv:
                for eventbus in self.targeteventbusmap.values():
                    eventbus.add_awaitables_to(self.awaitables)
                for awaitable in self.awaitables:
                    if isinstance(awaitable, asyncio.Task):
                        awaiting.add(awaitable)
                    else:
                        awaiting.add(asyncio.create_task(awaitable))
                self.awaitables = set()
            if not awaiting:
                await asyncio.sleep(1)
                continue
            (done, awaiting) = await asyncio.wait(awaiting, timeout=1,
                                                  return_when=asyncio.FIRST_COMPLETED)
            for d in done:
                r = d.result()
                if r:
                    awaiting.add(asyncio.create_task(r))

    def _update_cards(self, session, now):
        last = session.last_porter
        cards = self._retry_if_needed(session, lambda: session.get_cards())
        last['cards'] = { c['card']: c for c in cards }
        last['card_timestamp'] = now

    def _update_one_event(self, session, d):
        def getit(d, sub):
            r = d.get(sub)
            if r is None:
                r = {}
                d[sub] = r
            return r
        last = session.last_porter
        cards = last.get('cards')
        eventid = d['id']
        last['eventid'] = max(last['eventid'], eventid)
        low = d['description'].lower()
        lp = f"{d.get('logical', '')}/{d.get('physical', '')}"
        plp = f"{d.get('panel', '')}/{lp}"
        eventbus = self.targeteventbusmap[session.uri]
        if 'card found' in low:
            eventbus.propagate((d['name'], d['description'], plp))
            m = getit(last['cardfound'], 'vip' in low)
            self._increment(m, lp)
            codeint = int(d['code'])
            card = last.get('cards', {}).get(codeint)
            vip = ' VIP' if 'vip' in low else ''
            if card:
                LOGGER.info(f'{session.uri}{vip} {d["name"]} swiped {time.ctime(d["when"])}, previous {time.ctime(card["last_swiped"])}')
            else:
                LOGGER.info(f'{session.uri}{vip} new card {d["name"]} swiped {time.ctime(d["when"])}')
                last['cards'][codeint] = {
                    'card': int(d['code']),
                    'lastname': d['lastname'],
                }
            card['last_swiped'] = d['when']
        elif 'card not found' in low: # either not found or expired
            eventbus.propagate((d.get('name', ''), d['description'], plp))
            self._increment(last['cardnotfound'], lp)
            LOGGER.info(f'{session.uri} {d.get("name", "")} {d["description"]}')
        elif 'timezone violation' in low:
            eventbus.propagate((d['name'], d['description'], plp))
            self._increment(last['timezone'], lp)
            LOGGER.info(f'{session.uri} {d["name"]} timezone violation {time.ctime(d["when"])}')
            # TODO: should we update last_swiped?
        elif 'database update' in low:
            eventbus.propagate(('', d['description'], plp))
            LOGGER.warning(f'{session.uri} {low} {d}')
            self._increment(last['dbupdate'], lp)
        elif 'online' in low:
            eventbus.propagate(('', d['description'], plp))
            LOGGER.info(f'{session.uri} {low} {d}')
        elif 'tamper' in low or 'offline' in low or 'restarted' in low:
            eventbus.propagate(('', d['description'], plp))
            LOGGER.warning(f'{session.uri} {low}: {d}')
            self._increment(last, 'tamper')
        else:
            eventbus.propagate(('', d['description'], plp))
            LOGGER.warning(f'{session.uri} [unknown event type] {low} {d}')
            self._increment(last, 'unknowneventtypes')
        last['timestamp'] = max(d['when'], last['timestamp'])

    def _update_events(self, session):
        last = session.last_porter
        events = self._retry_if_needed(
            session, lambda: session.get_events(notbefore=last['timestamp']))
        maxcompletedeventid = last['eventid']
        for d in events:
            if d['id'] <= maxcompletedeventid:
                break  # they come in decreasing order, so we are done
            self._update_one_event(session, d)
        webevents = session.get_web_events(notbefore=last['timestamp'])
        for d in webevents:
            low = d['type'].lower()
            if low == 'invalid password' or low == 'unknown user':
                last['invalidpasswords'] += 1
            elif low == 'login':
                if 'Administrator' in d['description']:
                    last['adminlogins'] += 1
            elif low == 'logout':
                pass
            else:
                LOGGER.info(f'{target}: unknown webevent type {low}: {d}')

        # update timestamp with latest timestamp we got back
        allevents = events + webevents
        latest = max([e.get('when', 0) for e in allevents]) if allevents else 0
        last['timestamp'] = max(latest, last['timestamp'])

    @REQUEST_TIME.time()
    def collect(self, target):
        metric_to_gauge = {}
        def makegauge(metric, desc, labels=[]):
            already = metric_to_gauge.get(metric)
            if already:
                return already
            gmf = GaugeMetricFamily(metric, desc, labels=labels)
            metric_to_gauge[metric] = gmf
            return gmf

        with self.cv:
            session = self._get_session(target)
        with session.cv:
            last = session.last_porter
            now = time.time()
            if now - last['card_timestamp'] > self.config['netaxs']['card_refetch_interval']:
                self._update_cards(session, now)
                self._update_events(session)

            gmf = makegauge('successful_io_timestamp', 'when last successful I/O occurred')
            gmf.add_metric([], last['successful_io_timestamp']*1000)
            numvalid = sum([1 for c in last['cards'].values() if c.get('uses_remaining', 1) > 0 and c.get('expiration', now+10) > now])
            gmf = makegauge('num_access_cards', 'number of access cards in the system', ['valid'])
            gmf.add_metric(['1'], numvalid)
            gmf.add_metric(['0'], len(last['cards']) - numvalid)

            gmf_swiped = makegauge('card_last_swiped', 'when access card was last swiped', ['firstname', 'lastname'])
            gmf_expires = makegauge('card_expires', 'when access card expires', ['firstname', 'lastname'])
            for d in last['cards'].values():
                labels = [d.get('firstname', ''), d.get('lastname', '')]
                swipetime = d.get('last_swiped', 0)
                if swipetime:
                    gmf_swiped.add_metric(labels, swipetime)
                expires = d.get('expiration', 0)
                if expires:
                    gmf_expires.add_metric(labels, expires)

            gmf_invalid = makegauge('num_invalid_logins', 'how many invalid logins')
            gmf_invalid.add_metric([], last['invalidpasswords'])
            gmf_admin = makegauge('num_admin_logins', 'how many administrator logins')
            gmf_invalid.add_metric([], last['adminlogins'])
            # last['cardfound'] is keyed by is_vip and its value
            # is a dictionary keyed by lnpn whose value is the count.
            cmf_accepted = CounterMetricFamily(
                'cards_accepted',
                'number of card swipes that were accepted for access',
                labels=['vip', 'lnpn'], created=self.starttime
            )
            for is_vip in [True, False]:
                for (lnpn, count) in last['cardfound'].get(is_vip, {}).items():
                    labels = ['1' if is_vip else '0', lnpn]
                    cmf_accepted.add_metric(labels, count)

            cmf_rejected = CounterMetricFamily(
                'cards_rejected',
                'number of card swipes that were rejected',
                labels=['lnpn'], created=self.starttime
            )
            bad_lnpns = set(last['cardnotfound'].keys()).union(last['timezone'].keys())
            for lnpn in bad_lnpns:
                cmf_rejected.add_metric([lnpn], last['cardnotfound'].get(lnpn, 0) + last['timezone'].get(lnpn, 0))

            cmf_dbupdates = CounterMetricFamily(
                'num_database_updates',
                'number of database updates performed',
                labels=['lnpn'], created=self.starttime
            )
            for (lnpn, count) in last['dbupdates'].items():
                cmf_dbupdates.add_metric([lnpn], count)

            cmf_unknown = CounterMetricFamily(
                'num_unknown_events',
                'number of events with unknown eventtypes',
                labels=[], created=self.starttime
            )
            cmf_unknown.add_metric([], last['unknowneventtypes'])

            cmf_tamper = CounterMetricFamily(
                'num_tamper_events',
                'number of tamper events',
                labels=[], created=self.starttime
            )
            cmf_tamper.add_metric([], last['tamper'])

        return [g for g in metric_to_gauge.values()] + [cmf_accepted, cmf_rejected, cmf_dbupdates, cmf_unknown, cmf_tamper]


class EventbusStub:
    def __init__(self):
        pass
    def propagate(self, *args):
        pass
    def add_awaitables_to(self, otherset):
        pass

if __name__ == '__main__':
    import json, sys, yaml
    assert len(sys.argv) == 3, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    logging.basicConfig(level=logging.INFO)
    client = NetaxsClient(config, EventbusStub())
    target = sys.argv[2]

    # useful if there is some kind of unknown event and you want to see
    # what it looks like raw
    if False:
        session = client._get_session(target)
        ts = time.time() - 4*3600 # last 4 hours
        for e in session.get_events(notbefore=ts):
            print(e)
        for we in session.get_web_events(notbefore=ts):
            print('webevent', we)

    if True:
        LOGGER.setLevel(logging.DEBUG)
        session = client._get_session(target)
        while True:
            print(asyncio.run(session.read_event()))

    while True:
        i = client.collect(target)
        print(str(i))
        time.sleep(60)
