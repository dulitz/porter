
import copy, prometheus_client, time, yaml

from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY

import purpleair
from prometheus import start_wsgi_server


# see https://github.com/prometheus/client_python#custom-collectors

# we should also serve /metrics from prometheus_client directly
#      - requests and latency for PurpleAir, AmbientWeather, Neurio
#
# and / that contains a form for /probe along with recent statuses,
#
# and /config to dump the configuration

def registry_view_factory(parent, path, params):
    if not path.startswith('/probe'):
        return parent
    class ViewRestrictedRegistry(object):
        def __init__(self, parent, path, params):
            self.parent, self.path, self.params = parent, path, params
        def collect(self):
            collectors = None
            ti = None
            with parent._lock:
                collectors = copy.copy(parent._collector_to_names)
                if parent._target_info:
                    ti = parent._target_info_metric()
            if ti:
                yield ti

            for collector in collectors:
                collect2_func = None
                try:
                    collect2_func = collector.collect2
                except AttributeError:
                    pass
                if collect2_func:
                    for metric in collector.collect2(path, params):
                        yield metric
                else:
                    for metric in collector.collect():
                        yield metric
    return ViewRestrictedRegistry(parent, path, params)

# our request will be /probe?target=http://prometheus.io&module=neurio

class ProbeCollector(object):
    def __init__(self, config):
        self.config = config

    def collect(self):
        return iter([])
        
    def collect2(self, path, params):
        targets = params.get('target')
        rawmodule = params.get('module')
        module = rawmodule[0] if rawmodule and len(rawmodule) == 1 else ''
        if module and path.startswith('/probe'):
            if module == 'purpleair':
                for target in targets:
                    for metric in purpleair.collect(self.config, target):
                        yield metric
            elif module == 'neurio' or module == 'pwrview':
                self.collectNeurio(target)
            elif module == 'ambientweather':
                self.collectAmbientWeather(target)
            else:
                print('unknown module %s' % (params))
                yield GaugeMetricFamily('ignore', 'ignore')
        else:
            print('unknown request %s %s' % (path, params))
            yield GaugeMetricFamily('ignore', 'ignore')

    def collectNeurio(self, target):
        pass

    def collectAmbientWeather(self, target):
        pass

# config holds API keys for AmbientWeather

class Porter:
    def __init__(self, config):
        self.config = config
        REGISTRY.register(ProbeCollector(config))

    def start_wsgi_server(self, port=0):
        if not port:
            port = self.config.get('port', 8000)
        print('serving on port %d' % port)
        start_wsgi_server(port, registry_view_factory=registry_view_factory)        

def main(args):
    configfile = 'porter.yml'
    if len(args) > 1:
        configfile = args[1]

    config = yaml.safe_load(open(configfile, 'rt')) or {}
    p = Porter(config)
    p.start_wsgi_server()

    while True:
        time.sleep(1)

if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv))

# /probe&target=80845&module=purpleair
