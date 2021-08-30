"""
porter.py

Uses the multi-target exporter pattern described in
   https://prometheus.io/docs/guides/multi-target-exporter/
and
   https://github.com/prometheus/client_python#custom-collectors
to connect various external services to Prometheus.

The main advantage of this pattern is that the data is current at the time Prometheus
issues the query. Also you can control the frequency of queries, and what is queried,
by editing only the Prometheus configuration. This exporter can just run forever in a
container.

Currently supports these HVAC, lighting, and A/V platforms:
  Nest (HVAC), SmartThings, Savant, Lutron (Homeworks QS, Homeworks Illumination,
  Radio Ra 2, Radio Ra 2 Select, and Caseta PRO)

these security systems:
  Honeywell TotalConnect, Honeywell NetAXS-123

these electrical, water, propane, and fuel oil monitors:
  Flo by Moen, Neurio/Generac PWRview, Tank Utility, Schneider Conext Combox, Tesla

these temperature and air quality monitors:
  PurpleAir, Ambient Weather

these irrigation systems:
  Rachio

these hot water heaters:
  Rinnai

and these cars:
  Tesla

It's pretty easy to add a new module.

TODO: serve / with form for /probe along with recent statuses
  e.g. /probe&target=80845&module=purpleair
TODO: serve /config to dump the configuration
"""

import asyncio, json, logging, prometheus_client, requests, threading, sys, time, yaml
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY
from subprocess import TimeoutExpired

import ambientweather, combox, flo, lutron, nest, netaxs
import neurio, purpleair, rachio, rinnai, savant
import smartthings, tankutility, tesla, totalconnect

from brainstem import Brainstem
from sshproxy import SSHProxy
from prometheus import start_wsgi_server, SilentException

class RequestError(Exception):
    pass

LOGGER = logging.getLogger('porter')

BAD_REQUEST_COUNT = prometheus_client.Counter('porter_bad_requests',
                                              'number of bad requests')
READ_TIMEOUT_COUNT = prometheus_client.Counter('porter_read_timeouts',
                                               'number of read timeouts')
CONNECT_FAIL_COUNT = prometheus_client.Counter('porter_connect_failures',
                                               'number of connection failures')
BAD_RESPONSE_COUNT = prometheus_client.Counter('porter_bad_responses',
                                               'number of bad responses')
REQUEST_EXCEPTION_COUNT = prometheus_client.Counter('porter_request_exceptions', 'number of exceptions while processing a request')


class ProbeCollector(object):
    def __init__(self, config, sshproxy, module_to_client):
        self.config = config
        self.sshproxy = sshproxy
        self.module_to_client = module_to_client

    def collect(self):
        return iter([])

    @REQUEST_EXCEPTION_COUNT.count_exceptions()
    def _probe_collect2(self, module, targets):
        client = self.module_to_client.get(module)
        if client:
            return [client.collect(self.sshproxy.rewrite(t)) for t in targets]
        else:
            raise RequestError('unknown module %s' % module)

    def collect2(self, path, params):
        targets = params.get('target', [])
        rawmodule = params.get('module')
        module = rawmodule[0] if rawmodule and len(rawmodule) == 1 else ''

        if not targets:
            raise RequestError('no targets specified')
        try:
            if module and path.startswith('/probe'):
                for targetlist in self._probe_collect2(module, targets):
                    for metric in targetlist:
                        yield metric
                return # return now if everything went well
            else:
                raise RequestError('unknown request %s %s' % (path, params))
        except json.JSONDecodeError as e:
            BAD_RESPONSE_COUNT.inc()
            self.log(e, path, params)
        except requests.exceptions.ReadTimeout as e:
            READ_TIMEOUT_COUNT.inc()
            LOGGER.info(f'during {path} {params} caught ReadTimeout: {str(e)}')
            raise SilentException() # just fail the request, no more logging
        except requests.exceptions.ConnectionError as e:
            CONNECT_FAIL_COUNT.inc()
            for t in targets:
                if self.sshproxy.rewrite(t) != t:
                    self.sshproxy.restart_proxy_for(t)
            LOGGER.info(f'during {path} {params} caught ConnectionError: {str(e)}')
            raise SilentException() # just fail the request, no more logging
        except requests.exceptions.HTTPError as e:
            BAD_RESPONSE_COUNT.inc()
            LOGGER.info(f'during {path} {params} caught HTTPError: {str(e)}')
            raise SilentException() # just fail the request, no more logging
        except RequestError as e:
            BAD_REQUEST_COUNT.inc()
            self.log(e, path, params)
        except TimeoutExpired as e:
            BAD_RESPONSE_COUNT.inc()
            LOGGER.info(f'during {path} {params} caught subprocess.TimeoutExpired: {str(e)}')
            raise SilentException() # just fail the request, no more logging

        yield GaugeMetricFamily('ignore', 'ignore')

    def log(self, ex, path, params):
        """report an exception"""
        raise Exception('while processing %s %s, caught exception %s' % (path, params, ex))

class Porter:
    def __init__(self, config):
        self.config = config
        port = self.config.get('port')
        if not port:
            self.config['port'] = 8000
        self.sshproxy = SSHProxy(self.config)
        module_to_client = {}
        awaitables = set()
        bclient = Brainstem(self.config)
        if self.config.get('combox'):
            module_to_client['combox'] = combox.ComboxClient(self.config)
        if self.config.get('flo'):
            module_to_client['flo'] = flo.FloClient(self.config)
        if self.config.get('lutron'):
            lutronclient = lutron.LutronClient(self.config, bclient.module('lutron'))
            module_to_client['lutron'] = lutronclient
            awaitables.add(lutronclient.poll())
        if self.config.get('nest'):
            nestclient = nest.NestClient(self.config)
            module_to_client['nest'] = nestclient
        if self.config.get('netaxs'):
            nclient = netaxs.NetaxsClient(self.config, bclient.module('netaxs'))
            module_to_client['netaxs'] = nclient
            awaitables.add(nclient.poll())
        if self.config.get('rachio'):
            rclient = rachio.RachioClient(self.config)
            module_to_client['rachio'] = rclient
        if self.config.get('rinnai'):
            rinclient = rinnai.RinnaiClient(self.config)
            module_to_client['rinnai'] = rinclient
        if self.config.get('savant'):
            module_to_client['savant'] = savant.SavantClient(self.config, self.sshproxy.identityfiles)
        if self.config.get('smartthings'):
            module_to_client['smartthings'] = smartthings.SmartThingsClient(self.config)
        if self.config.get('tankutility'):
            tuclient = tankutility.TankUtilityClient(self.config)
            module_to_client['tankutility'] = tuclient
        if self.config.get('tesla'):
            tclient = tesla.TeslaClient(self.config)
            module_to_client['tesla'] = tclient
        if self.config.get('totalconnect'):
            tcclient = totalconnect.TCClient(self.config)
            module_to_client['totalconnect'] = tcclient
        ambientweather.config = self.config
        module_to_client['ambientweather'] = ambientweather
        neurio.config = self.config
        module_to_client['neurio'] = neurio
        module_to_client['pwrview'] = neurio
        purpleair.config = self.config
        module_to_client['purpleair'] = purpleair
        if self.config.get('brainstem'):
            # Brainstem uses the usual Prometheus client to expose its internals, so
            # you shouldn't really probe this from Prometheus. We just use this method
            # to expose event information to the browser.
            module_to_client['brainstem'] = bclient
            bclient.register_modules(module_to_client)
            awaitables |= bclient.get_awaitables()
        REGISTRY.register(ProbeCollector(self.config, self.sshproxy, module_to_client))
        if awaitables:
            def loop():
                async def async_loop():
                    awaiting = awaitables
                    LOGGER.info(f'started async polling loop, awaiting {len(awaiting)}')
                    while True:
                        (done, awaiting) = await asyncio.wait(awaiting, timeout=None, return_when=asyncio.FIRST_COMPLETED)
                        for d in done:
                            try:
                                r = d.result()
                            except Exception as exc:
                                LOGGER.critical(f'uncaught exception {exc} in async task {d}; exiting', exc_info=exc)
                                sys.exit(255)
                            if r:
                                awaiting.add(r)
                asyncio.run(async_loop())

            self.asyncthread = threading.Thread(target=loop, name="asyncio loop", daemon=True)
            self.asyncthread.start()

    def start_wsgi_server(self, port=0):
        if not port:
            port = self.config['port']
        LOGGER.info(f'serving on port {port}')
        start_wsgi_server(port)

    def terminate_proxies(self):
        # TODO: Currently when a proxy terminates, long-running clients will be
        # down until another probe comes in, because only probes can restart
        # proxies. This should really terminate and restart all the proxies.
        self.sshproxy.terminate()

def main(args):
    # we force the standard handler to be added, even though we have defined
    # our own handler in prometheus.py -- not sure it really works though
    logging.basicConfig(level=logging.INFO, force=True)
    configfile = 'porter.yml'
    if len(args) > 1:
        configfile = args[1]

    config = yaml.safe_load(open(configfile, 'rt')) or {}
    if config:
        LOGGER.info(f'using configuration file {configfile}')
    else:
        LOGGER.info(f'configuration file {configfile} was empty; ignored')
    p = Porter(config)
    p.start_wsgi_server()

    try:
        while True:
            time.sleep(1)
    except:
        p.terminate_proxies()


if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv))
