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
# currently supports PurpleAir, Ambient Weather, SmartThings, and Neurio/Generac PWRview.
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

import ambientweather, neurio, purpleair, savant, smartthings
from sshproxy import SSHProxy
from prometheus import start_wsgi_server


class ProbeCollector(object):
    def __init__(self, config, sshproxy, stclient, savantclient):
        self.config = config
        self.sshproxy = sshproxy
        self.smartthings = stclient
        self.savant = savantclient

    def collect(self):
        return iter([])
        
    def collect2(self, path, params):
        targets = params.get('target', [])
        rawmodule = params.get('module')
        module = rawmodule[0] if rawmodule and len(rawmodule) == 1 else ''
        if module and path.startswith('/probe'):
            try:
                if module == 'purpleair':
                    for target in targets:
                        for metric in purpleair.collect(self.config, self.sshproxy.rewrite(target)):
                            yield metric
                elif module == 'ambientweather':
                    for target in targets:
                        for metric in ambientweather.collect(self.config, self.sshproxy.rewrite(target)):
                            yield metric
                elif module == 'smartthings':
                    for target in targets:
                        for metric in self.smartthings.collect(self.sshproxy.rewrite(target)):
                            yield metric
                elif module == 'neurio' or module == 'pwrview':
                    for target in targets:
                        for metric in neurio.collect(self.config, self.sshproxy.rewrite(target)):
                            yield metric
                elif module == 'savant':
                    for target in targets:
                        for metric in self.savant.collect(self.sshproxy.rewrite(target)):
                            yield metric
                else:
                    print('unknown module %s' % (params))
                    yield GaugeMetricFamily('ignore', 'ignore')
            except json.JSONDecodeError:
                raise
            except requests.exceptions.HTTPError:
                raise
        else:
            print('unknown request %s %s' % (path, params))
            yield GaugeMetricFamily('ignore', 'ignore')


class Porter:
    def __init__(self, config):
        self.config = config
        port = self.config.get('port')
        if not port:
            self.config['port'] = 8000
        self.sshproxy = SSHProxy(self.config)
        stclient = smartthings.SmartThingsClient(self.config) if self.config.get('smartthings') else None
        savantclient = savant.SavantClient(self.config) if self.config.get('savant') else None
        REGISTRY.register(ProbeCollector(config, self.sshproxy, stclient, savantclient))

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
