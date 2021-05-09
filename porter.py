# porter.py
#
# uses the multi-target exporter pattern -- see
#    https://prometheus.io/docs/guides/multi-target-exporter/
# and
#    https://github.com/prometheus/client_python#custom-collectors
# -- to connect various external services to Prometheus.
#
# the main advantage of this pattern is that the data is current at the time Prometheus
# issues the query. also you can control the frequency of queries, and what is queried,
# by editing only the Prometheus configuration. this exporter can just run forever in a
# container.
#
# currently supports PurpleAir, Ambient Weather, SmartThings, Neurio/Generac PWRview,
# Savant, and Flo by Moen.
#
# e.g. /probe&target=80845&module=purpleair

# TODO: response code histograms
# TODO: histograms of exceptions at top level
# TODO: serve / with form for /probe along with recent statuses
# TODO: serve /config to dump the configuration
# TODO: move from hardcoded to config file


import json, prometheus_client, requests, time, yaml
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY

import ambientweather, flo, neurio, purpleair, savant, smartthings
from sshproxy import SSHProxy
from prometheus import start_wsgi_server


class RequestError(Exception):
    pass

BAD_REQUEST_COUNT = prometheus_client.Counter('porter_bad_requests', 'number of bad requests')
CONNECT_FAIL_COUNT = prometheus_client.Counter('porter_connect_failures',
                                               'number of connection failures')
BAD_RESPONSE_COUNT = prometheus_client.Counter('porter_bad_responses', 'number of bad responses')


class ProbeCollector(object):
    def __init__(self, config, sshproxy, stclient, savantclient, floclient):
        self.config = config
        self.sshproxy = sshproxy
        self.smartthings = stclient
        self.savant = savantclient
        self.flo = floclient

    def collect(self):
        return iter([])

    def _probe_collect2(self, module, targets):
        if module == 'purpleair':
            return [purpleair.collect(self.config, self.sshproxy.rewrite(t)) for t in targets]
        elif module == 'ambientweather':
            return [ambientweather.collect(self.config, self.sshproxy.rewrite(t)) for t in targets]
        elif module == 'smartthings':
            return [self.smartthings.collect(self.sshproxy.rewrite(t)) for t in targets]
        elif module == 'neurio' or module == 'pwrview':
            return [neurio.collect(self.config, self.sshproxy.rewrite(t)) for t in targets]
        elif module == 'savant':
            return [self.savant.collect(self.sshproxy.rewrite(t)) for t in targets]
        elif module == 'flo':
            return [self.flo.collect(self.sshproxy.rewrite(t)) for t in targets]
        else:
            raise RequestError('unknown module %s' % module)

    def collect2(self, path, params):
        targets = params.get('target', [])
        rawmodule = params.get('module')
        module = rawmodule[0] if rawmodule and len(rawmodule) == 1 else ''

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
        except requests.exceptions.HTTPError as e:
            BAD_RESPONSE_COUNT.inc()
            self.log(e, path, params)
        except requests.exceptions.ConnectionError as e:
            CONNECT_FAIL_COUNT.inc()
            for t in targets:
                if self.sshproxy.rewrite(t) != t:
                    self.sshproxy.restart_proxy_for(t)
            self.log(e, path, params)
        except RequestError as e:
            BAD_REQUEST_COUNT.inc()
            self.log(e, path, params)
        assert False # self.log() should have raised
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
        stclient = smartthings.SmartThingsClient(self.config) if self.config.get('smartthings') else None
        savantclient = savant.SavantClient(self.config, self.sshproxy.identityfiles) if self.config.get('savant') else None
        floclient = flo.FloClient(self.config) if self.config.get('flo') else None
        REGISTRY.register(ProbeCollector(config, self.sshproxy, stclient, savantclient, floclient))

    def start_wsgi_server(self, port=0):
        if not port:
            port = self.config['port']
        print('serving on port %d' % port)
        start_wsgi_server(port)

    def terminate_proxies(self):
        self.sshproxy.terminate()

def main(args):
    configfile = 'porter.yml'
    if len(args) > 1:
        configfile = args[1]

    config = yaml.safe_load(open(configfile, 'rt')) or {}
    if config:
        print('using configuration file %s' % configfile)
    else:
        print('configuration file %s was empty; ignored' % configfile)
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
