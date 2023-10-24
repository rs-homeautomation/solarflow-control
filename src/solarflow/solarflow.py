from paho.mqtt import client as mqtt_client
from datetime import datetime, timedelta
from functools import reduce
import logging
import json
import sys
from utils import TimewindowBuffer

red = "\x1b[31;20m"
reset = "\x1b[0m"
FORMAT = '%(asctime)s:%(levelname)s: %(message)s'
logging.basicConfig(stream=sys.stdout, level="INFO", format=FORMAT)
log = logging.getLogger("")

class SolarflowHub:

    def __init__(self, device_id: str, client: mqtt_client, window:int = 5):
        self.client = client
        self.device_id = device_id
        self.solarInputValues = TimewindowBuffer()
        self.solarInputPower = -1       # solar input power of connected panels
        self.outputPackPower = 0        # charging power of battery pack 
        self.packInputPower = 0         # discharging power of battery pack
        self.outputHomePower = -1       # power sent to home

        self.electricLevel = -1         # state of charge of battery pack
        self.batteries = {"none":-1}    # state of charge for individual batteries
        self.outputLimit = -1           # power limit for home output
        self.lastFullTS = None          # keep track of last time the battery pack was full (100%)
        self.lastEmptyTS = None         # keep track of last time the battery pack was empty (0%)
        self.lastSolarInputTS = None    # time of the last received solar input value

        self.property_topic = f'iot/73bkTV/{device_id}/properties/write'

    def __str__(self):
        #solar = ",".join([f'{v:>4}' for v in self.solarInputValues])
        #batteries = "|".join("{}%".format(v) for k, v in self.batteries.items())
        batteries = "|".join([f'{v:>2}%' for v in self.batteries.values()])
        return ' '.join(f'{red}HUB: \
                        S:{self.solarInputPower:>3.1f}W {self.solarInputValues}, \
                        B:{self.electricLevel:>3}% ({batteries}), \
                        C:{self.outputPackPower-self.packInputPower:>4}W, \
                        F:{self.getLastFullBattery():3.1f}h, \
                        E:{self.getLastEmptyBattery():3.1f}h, \
                        H:{self.outputHomePower:>3}W, \
                        L:{self.outputLimit:>3}W{reset}'.split())

    def subscribe(self):
        topics = [
            "solarflow-hub/telemetry/solarInputPower",
            "solarflow-hub/telemetry/electricLevel",
            "solarflow-hub/telemetry/outputPackPower",
            "solarflow-hub/telemetry/packInputPower",
            "solarflow-hub/telemetry/outputHomePower",
            "solarflow-hub/telemetry/outputLimit",
            "solarflow-hub/telemetry/batteries/+/socLevel"
        ]
        for t in topics:
            self.client.subscribe(t)


    def updSolarInput(self, value:int):
        self.solarInputValues.add(value)
        self.solarInputPower = self.solarInputValues.wavg()
        self.lastSolarInputTS = datetime.now()
    
    def updElectricLevel(self, value:int):
        if value == 100:
            self.lastFullTS = datetime.now()
        if value == 0:
            self.lastEmptyTS = datetime.now()
        self.electricLevel = value
    
    def updOutputPack(self, value:int):
        self.outputPackPower = value

    def updPackInput(self, value:int):
        self.packInputPower = value

    def updOutputHome(self, value:int):
        self.outputHomePower = value
    
    def updOutputLimit(self, value:int):
        self.outputLimit = value
    
    def updBatterySoC(self, sn:str, value:int):
        self.batteries.pop("none",None)
        self.batteries.update({sn:value})

    # handle content of mqtt message and update properties accordingly
    def handleMsg(self, msg):
        if msg.topic.startswith('solarflow-hub') and msg.payload:
            # check if we got regular updates on solarInputPower
            # if we haven't received any update on solarInputPower for 120s
            # we assume it's not producing and inject 0
            now = datetime.now()
            if self.lastSolarInputTS:
                diff = now - self.lastSolarInputTS
                seconds = diff.total_seconds()
                if seconds > 120:
                    self.updSolarInputPower(0)

            metric = msg.topic.split('/')[-1]
            value = int(msg.payload.decode())
            match metric:
                case "electricLevel":
                    self.updElectricLevel(value)
                case "solarInputPower":
                    self.updSolarInput(value)
                case "outputPackPower":
                    self.updOutputPack(value)
                case "packInputPower":
                    self.updPackInput(value)
                case "outputHomePower":
                    self.updOutputHome(value)
                case "outputLimit":
                    self.updOutputLimit(value)
                case "socLevel":
                    sn = msg.topic.split('/')[-2]
                    self.updBatterySoC(sn=sn, value=value)
                case _:
                    log.warning(f'Ignoring solarflow-hub metric: {metric}')

    def setOutputLimit(self, limit:int):
        if limit < 0:
            limit = 0
        # currently the hub doesn't support single steps for limits below 100
        # to get a fine granular steering at this level we need to fall back to the inverter limit
        # if controlling the inverter is not possible we should stick to either 0 or 100W
        if limit <= 100:
            #limitInverter(client,limit)
            #log.info(f'The output limit would be below 100W ({limit}W). Would need to limit the inverter to match it precisely')
            m = divmod(limit,30)[0]
            r = divmod(limit,30)[1]
            limit = 30 * m + 30 * (r // 15)

        outputlimit = {"properties": { "outputLimit": limit }}
        self.client.publish(topic_limit_solarflow,json.dumps(outputlimit))
        log.info(f'Setting solarflow output limit to {limit} W')
        return limit

    def setBuzzer(self, state: bool):
        buzzer = {"properties": { "buzzerSwitch": 0 if not state else 1 }}
        self.client.publish(topic_limit_solarflow,json.dumps(buzzer))

    # return how much time has passed since last full charge (in hours)
    def getLastFullBattery(self) -> int:
        if self.lastFullTS:
            diff = datetime.now() - self.lastFullTS
            return diff.total_seconds()/3600
        else:
            return -1

    # return how much time has passed since last full charge (in hours)
    def getLastEmptyBattery(self) -> int:
        if self.lastEmptyTS:
            diff = datetime.now() - self.lastEmptyTS
            return diff.total_seconds()/3600
        else:
            return -1

