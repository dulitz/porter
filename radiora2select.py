# radiora2select.py
#
# the Lutron Radio Ra2 Select module for porter, the Prometheus exporter
#
# see https://github.com/upsert/liplib
# and https://www.lutron.com/TechnicalDocumentLibrary/040249.pdf


import asyncio, json, liplib, prometheus_client, time, threading

from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily

REQUEST_TIME = prometheus_client.Summary('radiora2select_processing_seconds',
                                         'time of Radio Ra2 Select requests')

# for each target we have one "press_actions" metric, a counter, with labels
# "area", "deviceid", "name", "button", and "scene_number". we have one
# "output_levels" metric, a gauge, with labels "area", "deviceid", "name".
#
# we increment the counter when we see ~DEVICE. for deviceid 1, the scene_number is
# taken from the component and the name is the scene name configured for that number;
# area and button are empty. for other deviceids, scene_number is empty and button is
# taken from the component.
#
# we set the gauge when we see ~OUTPUT.


# what we can do:
#   observe press and release of Pico buttons:
#      ~DEVICE,28,2,3
#      ~DEVICE,28,2,4
#         [various ~OUTPUT changes from the button action]
#   observe activation of scenes:
#      ~DEVICE,1,1,3
#         [various ~OUTPUT changes from the scene]
#      ~DEVICE,1,1,4
#   observe changes to output levels, e.g. from the app:
#      ~OUTPUT,23,1,0.00
#   query the output level of a dimmer:
#      ?output,26,1
#      returns ~OUTPUT,26,1,100.0
#   press and release a Pico button causing it to activate its associated dimmers:
#      #device,28,2,3
#      #device,28,2,4
#   returns ~OUTPUT,26,1,100.00
#           ~OUTPUT,27,1,100.00
#           ~OUTPUT,23,1,100.00
#   run a scene
#      #device,1,2,3
#      #device,1,2,4

# TODO: when we start, we should query the output level of all our dimmers

class Lipservice:
    def __init__(self, host, port, raconfig, client):
        self.lipserver = liplib.LipServer()
        self.host, self.port = host, port
        self.raconfig = raconfig
        self.client = client
        
        self.cmf = CounterMetricFamily(
            'press_actions', 'count of button presses and scene activations',
            labels=['deviceid', 'name', 'button', 'area', 'scene_number'], created=time.time()
        )
        self.outputlevels = {}
        for deviceid in self.client.deviceid_to_dimmertuple.keys():
            # we initialize the dict so its key iterator won't be invalidated by
            # insertions from other threads.
            self.outputlevels[deviceid] = None

        print('new Lutron connection to %s:%d' % (host, port))

    async def open(self):
        await self.lipserver.open(self.host, self.port,
                                  username=self.raconfig['user'].encode(),
                                  password=self.raconfig['password'].encode())

    async def query_levels(self):
        await self.open()
        for deviceid in self.outputlevels:
            await self.lipserver.query('OUTPUT', deviceid, 1)

    async def poll(self):
        (a, b, c, d) = await self.lipserver.read()
        if a is None:
            await self.open()
        elif a == 'DEVICE':
            deviceid, component, action = b, c, d
            if action == liplib.LipServer.Button.PRESS:
                if deviceid == 1: # then a scene was triggered
                    name = self.client.sceneid_to_name.get(component, '')
                    # list append is thread safe
                    self.cmf.add_metric(['', name, '', '', str(component)],
                                        1, timestamp=time.time())
                else: # a standalone device
                    (name, area, buttons) = self.client.deviceid_to_sensortuple.get(deviceid, (None, None, None))
                    if name is None:
                        (name, area) = self.client.deviceid_to_dimmertuple.get(deviceid, (None, None))
                    # list append is thread safe
                    self.cmf.add_metric([str(deviceid), name, str(component), area],
                                        1, timestamp=time.time())
        elif a == 'OUTPUT':
            deviceid, action, level = b, c, d
            if action == liplib.LipServer.Action.SET:
                self.outputlevels[deviceid] = level
            elif action == liplib.LipServer.Action.RAISING:
                # valid response, we just don't have shades so this class doesn't support it
                print('shade actions not supported for device %d' % deviceid)
            elif action == liplib.LipServer.Action.LOWERING:
                # valid response, we just don't have shades so this class doesn't support it
                print('shade actions not supported for device %d' % deviceid)
            elif action == liplib.LipServer.Action.STOP:
                pass
            else:
                print('unknown ~OUTPUT action %d for deviceid %d' % (action, deviceid))
        elif a == 'ERROR':
            print('~ERROR while polling: %s %s %s' % (b, c, d))
        else:
            print('unknown response while polling: %s %s %s %s' % (a, b, c, d))
        return self.poll() # return ourselves as coroutine so we are restarted

class LipserviceManager:
    def __init__(self):
        self.cv = threading.Condition()
        self.target_to_raconfig = {}
        self.hostport_to_lipservice = {}
        self.tasks_pending = set()

    def register_target(self, target, raconfig, client):
        with self.cv:
            self.target_to_raconfig[target] = (raconfig, client)

    def get_lipservice_for_target(self, target):
        (host, colon, portstr) = target.partition(':')
        port = int(portstr or 23)
        return self.hostport_to_lipservice.get((host, port))
        
    async def poll(self, timeout=1):
        with self.cv:
            targets = [t for t in self.target_to_raconfig.items()]
        for (target, t) in targets:
            (raconfig, client) = t
            (host, colon, portstr) = target.partition(':')
            port = int(portstr or 23)
            lips = self.hostport_to_lipservice.get((host, port))
            if not lips:
                lips = Lipservice(host, port, raconfig, client)
                self.hostport_to_lipservice[(host, port)] = lips
                asyncio.create_task(lips.query_levels()) # only do this once
                self.tasks_pending.add(asyncio.create_task(lips.poll()))
        if self.tasks_pending:
            (done, self.tasks_pending) = await asyncio.wait(self.tasks_pending, timeout=timeout,
                                                            return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                # as each poll() completes, schedule it to run again
                self.tasks_pending.add(asyncio.create_task(task.result()))
        else:
            await asyncio.sleep(timeout)


class RadioRa2SelectClient:
    def __init__(self, config):
        self.config = config
        self.manager = LipserviceManager()
        raconfig = config.get('radiora2select')
        if not raconfig:
            raise Exception('no radiora2select configuration')
        if not raconfig.get('user'):
            raconfig['user'] = 'lutron'
        if not raconfig.get('password'):
            raconfig['password'] = 'integration'
        integration_json_string = raconfig.get('integration')
        if integration_json_string:
            self.process_integration_report(json.loads(integration_json_string))
        else:
            self._process_integration_yaml()
        self.asyncthread = threading.Thread(target=self.asyncio_loop, name="RadioRa2SelectClient asyncio loop", daemon=True)
        self.asyncthread.start()

    def asyncio_loop(self):
        async def loop():
            print('RadioRa2Select beginning async polling')
            while True:
                await self.manager.poll()
        asyncio.run(loop())

    def process_integration_report(self, js):
        """Reads an integration report that was generated by the Lutron app for
        Radio Ra 2 Select. This overwrites any existing integration information from
        the config file or previous calls to this method.
        """
        lipidlist = js.get('LIPIdList')
        assert lipidlist, js

        self.sceneid_to_name = {} # all scenes are components of deviceid 1
        self.deviceid_to_sensortuple = {} # sensors (Pico or Occupancy Sensor)
        self.deviceid_to_dimmertuple = {}
        self.areaname_to_devices = {}
        def add_device(areaname, t):
            lis = self.areaname_to_devices.get(areaname)
            if not lis:
                lis = []
                self.areaname_to_devices[areaname] = lis
            lis.append(t)

        for device in lipidlist.get('Devices', []):
            if device['ID'] == 1: # then device is the Smart Bridge
                for button in device.get('Buttons', []):
                    name = button['Name']
                    if not name.startswith('Button '):
                        # then this "button" is a scene
                        self.sceneid_to_name[button['Number']] = name
            else: # then device is a sensor (Pico or occupancy sensor)
                areaname = device.get('Area', {}).get('Name', '')
                buttons = [b['Number'] for b in device.get('Buttons', [])]
                self.deviceid_to_sensortuple[device['ID']] = (device['Name'], areaname, buttons)
                add_device(areaname, (device['ID'], device['Name'], buttons))

        for device in lipidlist.get('Zones', []):
            areaname = device.get('Area', {}).get('Name', '')
            self.deviceid_to_dimmertuple[device['ID']] = (device['Name'], areaname)
            add_device(areaname, (device['ID'], device['Name']))

    def _process_integration_yaml(self):
        """This processes the scenes and areas maps from the config to create the
        deviceid maps. Never needs to be called by the client.
        """
        raconfig = self.config.get('radiora2select')
        self.sceneid_to_name = raconfig.get('scenes', {})
        self.areaname_to_devices = raconfig.get('areas', {})
        self.deviceid_to_dimmertuple = {}
        self.deviceid_to_sensortuple = {}
        for (areaname, devicelist) in self.areaname_to_devices.items():
            for (deviceid, devicename, *buttons) in devicelist:
                if not buttons:
                    self.deviceid_to_dimmertuple[deviceid] = (devicename, areaname)
                else:
                    assert len(buttons) == 1, (deviceid, devicename, buttons)
                    self.deviceid_to_sensortuple[deviceid] = (devicename, areaname, buttons[0])

    def dump_integration_yaml_string(self):
        """Returns a YAML format string that reflects the integration. It's more compact
        and easier to read than the integration JSON.
        """
        out = ['radiora2select:']
        out.append('  scenes:')
        for (sceneid, name) in sorted(self.sceneid_to_name.items()):
            out.append("    %d: '%s'" % (sceneid, name))
        out.append('  areas:')
        for (areaname, t) in sorted(self.areaname_to_devices.items()):
            lis = ['[%s]' % ', '.join([str(devid), "'%s'" % dname] + [str(b) for b in buttons]) for (devid, dname, *buttons) in t]
            out.append(("    '%s': [" % areaname) + ', '.join(lis) + ']')
        return '\n'.join(out)
    
    @REQUEST_TIME.time()
    def collect(self, target):
        """request all the matching devices and get the status of each one"""

        raconfig = self.config['radiora2select']
        self.manager.register_target(target, raconfig, self)
        lips = self.manager.get_lipservice_for_target(target)
        if not lips:
            return []
        gmf = GaugeMetricFamily('output_level_pct', 'current output level (% of full output)',
                                labels=['deviceId', 'name', 'area'])
        for deviceid in lips.outputlevels.keys():
            level = lips.outputlevels[deviceid]
            if level is not None:
                (name, area) = self.deviceid_to_dimmertuple.get(deviceid, ('', ''))
                gmf.add_metric([str(deviceid), name, area], level)
        return [gmf, lips.cmf]

if __name__ == '__main__':
    import sys, yaml
    assert len(sys.argv) == 3, sys.argv
    config = yaml.safe_load(open(sys.argv[1]))
    client = RadioRa2SelectClient(config)

    js = json.load(open(sys.argv[2], 'rt')) # the integration report from the bridge
    client.process_integration_report(js)
    s = client.dump_integration_yaml_string()
    print(s)

    #devices = liplib.load_integration_report(js)
    #print(json.dumps(devices, indent=2))
