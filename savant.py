#!/usr/bin/python

# if target is localhost:/path/to/sclibridge then run sclibridge locally, otherwise ssh to target
# and run /path/to/sclibridge. if there is no colon in target, path defaults to "sclibridge".

#    sclibridge statenames
#    sclibridge userzones
#    sclibridge getSceneNames

printonly = False
###printonly = True

MAX_SCLI_STATES_PER_COMMAND = 99

class SavantError(Exception):
    pass

import itertools, prometheus_client, subprocess, shlex, threading
from datetime import datetime
from prometheus_client.core import GaugeMetricFamily

REQUEST_TIME = prometheus_client.Summary('savant_processing_seconds',
                                         'time of savant requests')

class SavantProcess:
    def write(self, states, values):
        parsedcommand = ["ssh", self.host, self.sclipath, "writestate"]
        for (state, value) in zip(states, values):
            parsedcommand += [shlex.quote(state), shlex.quote(value)]
        completed = subprocess.run(parsedcommand,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   timeout=10,
                                   universal_newlines=True)
        if completed.stderr: print("ssh exited with non-null stderr", completed.stderr)
        if completed.stdout: print("ssh existed with non-null stdout", completed.stdout)
        completed.check_returncode()

    def servicerequest(self, commandlist):
        parsedcommand = ["ssh", self.host, self.sclipath, "servicerequestcommand"] + [shlex.quote(v) for v in commandlist]
    
    def read(self, states):
        self.states = states
        if not states:
            self.results = []
            return
        assert len(states) <= MAX_SCLI_STATES_PER_COMMAND

        escstates = [shlex.quote(state) for state in states]
        parsedcommand = ['ssh', self.host, self.sclipath, 'readstate'] + escstates
        completed = subprocess.run(parsedcommand,
                                   stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   timeout=10)
        if completed.stderr: print('ssh exited with non-null stderr', completed.stderr)
        completed.check_returncode()
        result_string = completed.stdout.decode('utf-8')
        if result_string[-1] == '\n':
            # as it really ought to be almost all the time
            result_string = result_string[:-1] # eliminate empty split at the end
        self.results = result_string.split('\n')
        if len(self.results) != len(states):
            print(states)
            print(self.results)
            print(result_string)
            raise SavantError('%d inputs but %d outputs' % (len(states), len(self.results)))


class SavantClient:
    def __init__(self, config, sshidentities=[]):
        self.config = config
        self.sshport = config.get('savant', {}).get('sshport')
        self.sshidentities = sshidentities

        # self.target_seen maps target to a structure with members userzones,
        # scenenames, and statenames.
        self.cache_cv = threading.Condition()
        self.target_seen = {}

    def _sclibridge(self, target, subcommand, arglist=[]):
        # target is of the form RPM@10.188.2.250:/usr/local/bin/sclibridge
        (host, colon, path) = target.rpartition(':')
        if not host:
            (host, path) = (path, 'sclibridge')
        if host == 'localhost':
            the_command = [path, subcommand] + arglist
        else:
            parg = ['-p%d' % self.sshport] if self.sshport else []
            iargs = ['-i%s' % f for f in self.sshidentities]
            the_command = ['ssh'] + parg + iargs + ['-anxT', '-oServerAliveInterval=10', '-oBatchMode=yes', '-oClearAllForwardings=yes', host, path, subcommand] + [shlex.quote(v) for v in arglist]
        return subprocess.run(the_command, encoding='utf-8', check=True,
                              capture_output=True, timeout=20)

    def _initialize_target(self, target):
        class Target:
            pass
        t = Target()
        t.userzones = dict([(z, 1) for z in self._sclibridge(target, 'userzones').stdout.splitlines()])
        t.scenenames = [line.split(',') for line in self._sclibridge(target, 'getSceneNames').stdout.splitlines()]
        t.statenames = []
        states = [(line, 'between') for line in self._sclibridge(target, 'statenames').stdout.splitlines()]
        prefixes = self.config.get('savant', {}).get('prefixes')
        if prefixes:
            suffixes = self.config.get('savant', {}).get('suffixes', [])
            for prefixspec in prefixes:
                if type(prefixspec) == type(''):
                    states.append((prefixspec, 'beginAndEnd'))
                else:
                    states.append((prefixspec[0], 'begin'))
                    states.append((prefixspec[1], 'end'))
            states.sort()
            current_prefix = None
            for (line, condition) in states:
                if condition == 'end':
                    current_prefix = None
                elif condition == 'begin' or condition == 'beginAndEnd':
                    current_prefix = line
                elif current_prefix:
                    assert condition == 'between', condition
                    if line.startswith(current_prefix):
                        t.statenames.append(line) # accept it
                    else:
                        current_prefix = None
                if current_prefix is None and condition == 'between':
                    # prefix rules reject this -- do suffix rules accept?
                    for suffix in suffixes:
                        if line.endswith(suffix):
                            t.statenames.append(line) # accept it
                            break
        else: # no prefixes specified so accept them all
            t.statenames = [line for (line, condition) in states]
        t.statenames.sort()
        with self.cache_cv:
            self.target_seen[target] = t
        return t

    def get_state_values(self, target):
        t = self.target_seen.get(target) or self._initialize_target(target)
        args = [iter(t.statenames)] * MAX_SCLI_STATES_PER_COMMAND
        accum = []
        for chunk in itertools.zip_longest(*args, fillvalue=None):
            names = [name for name in chunk if name]
            values = self._sclibridge(target, 'readstate', names).stdout.splitlines()
            assert len(names) == len(values), (len(names), len(values), names, values)
            accum += zip(names, values)
        return accum
        
    @REQUEST_TIME.time()
    def collect(self, target):
        gauges = []
        statevalues = self.get_state_values(target)
        userzones = self.target_seen.get(target).userzones
        for (name, value) in statevalues:
            segments = name.split('.')
            (labelnames, labelvalues, metricname) = self._labels_from_segments(segments, userzones)
            v = self._convert_value(value)
            if type(v) == type(''):
                labelnames.append('state')
                labelvalues.append(v)
                v = 1
            if v is not None:
                metricname = metricname.replace(' ', '')
                gmf = GaugeMetricFamily(metricname, 'Savant statename %s' % name, labels=labelnames)
                gmf.add_metric(labelvalues, v)
                gauges.append(gmf)
        
        return gauges

    def _parse_segment(self, n, met):
        if met.find('_') >= 0:
            for n in range(0, len(met)):
                c = met[-(n+1)]
                if c != '_' and not c.isdigit():
                    if n > 1 and met[-n] == '_':
                        return (met[:-n], met[-(n-1):])
                    break
        names = ['subcomponent', 'microcomponent', 'nanocomponent']
        return (names[n], met)

    def _labels_from_segments(self, segments, userzones):
        if len(segments) == 6:
            # then this is a service
            assert segments[0] in userzones, (segments, userzones)
            labelnames = ['userzone', 'component', 'logical', 'variant', 'service']
            labelvalues = segments[0:-1]
        elif len(segments) > 2:
            labelnames = ['userzone' if segments[0] in userzones else 'component']
            labelvalues = [segments[0]]
            for (n, segment) in enumerate(segments[1:-1]):
                (labelname, labelvalue) = self._parse_segment(n, segment)
                labelnames.append(labelname)
                labelvalues.append(labelvalue)
        elif len(segments) == 2:
            if segments[0]:
                labelnames = ['userzone' if segments[0] in userzones else 'component']
                labelvalues = [segments[0]]
            # else ignore the empty segment
        else:
            assert len(segments) == 1, segments
            labelnames, labelvalues = [], []
        met = segments[-1]
        if met.find('_') >= 0:
            for n in range(0, len(met)):
                c = met[-(n+1)]
                if c != '_' and not c.isdigit():
                    if n > 1 and met[-n] == '_':
                        labelnames.append('unit')
                        unit = met[-(n-1):]
                        labelvalues.append(unit)
                        d = dict(zip(labelnames, labelvalues))
                        component = d.get('component')
                        if component and not d.get('name'):
                            cd = self.config.get('savant', {}).get('names', {}).get(component, {})
                            unitname = cd.get(unit)
                            if cd and not unitname:
                                try:
                                    unitname = cd.get(int(unit))
                                except ValueError:
                                    pass
                            if unitname:
                                labelnames.append('name')
                                labelvalues.append(unitname)
                        return (labelnames, labelvalues, met[:-n])
                    break
        return (labelnames, labelvalues, met)
    
    # Audio Matrix.AV_switch_1.IsCurrentAudioValidStatus_25
    # HVAC Controller.HVAC_controller.CurrentHVACMode_1_1
    #
    # Entry & Library.SavantCast.Audio Source.1.SVC_AV_GENERALAUDIO.CurrentVolume
    # Entry & Library.Side Yard.Security_camera.1.SVC_ENV_SECURITYCAMERA.ServiceIsActive
    # Exterior & Garage.Lighting Controller.Lighting_controller.1.SVC_ENV_LIGHTING.ServiceIsActive
    # Exterior & Garage.Media Server.Player_B.1.SVC_AV_LIVEMEDIAQUERY_SAVANTMEDIAAUDIO.ServiceState
    #
    # Guest Suite.RoomNumberOfLightsOn
    #
    # ServiceIsActive{zone="Entry & Library",component="Side Yard",logical="Security_camera",variant="1",service="SVC_ENV_SECURITYCAMERA"}
    # ServiceState{zone="Exterior & Garage",component="Media Server",logical="Player_B",variant="1",service="SRC_AV_LIVEMEDIAQUERY_SAVANTMEDIAAUDIO"}

    def _convert_value(self, v):
        lowerv = v.lower()
        if v == '' or lowerv == 'none' or v == '-' or v.startswith('NAK'):
            return None
        if lowerv == 'no' or lowerv == 'off' or lowerv == 'disconnected' or lowerv == 'inactive' or lowerv == 'closed':
            return 0
        try:
            return float(v)
        except ValueError:
            pass
        if v[-1] == '%':
            try:
                return float(v[:-1])
            except ValueError:
                pass
        try:
            return datetime.strptime(v, '%Y-%m-%d %H:%M:%S %z').timestamp() # 2021-05-07 15:59:54 -0700
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(v).timestamp()
        except ValueError:
            pass
        return v


if __name__ == '__main__':
    import sys, yaml
    assert len(sys.argv) == 3, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    client = SavantClient(config)
    for (name, value) in client.get_state_values(sys.argv[2]):
        print('%20s' % value, '\t', name)
        userzones = client.target_seen.get(sys.argv[2]).userzones
        segments = name.split('.')
        (labelnames, labelvalues, metricname) = client._labels_from_segments(segments, userzones)
        v = client._convert_value(value)
        if type(v) == type(''):
            labelnames.append('state')
            labelvalues.append(v)
            v = 1
        metricname = metricname.replace(' ', '')
        labels = ['%s="%s"' % (n, v) for (n, v) in zip(labelnames, labelvalues)]
        print('%s{%s} = %s' % (metricname, ','.join(labels), v))
