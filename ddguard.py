#!/usr/bin/env python3
###############################################################################
#  
#  Diabetes Data Guard (DD-Guard): Gateway module
#  
#  Description:
#
#    The DD-Guard gateway module periodically receives real time data from the 
#    Medtronic Minimed 670G insulin pump and uploads it to the cloud service
#  
#  Dependencies:
#
#    DD-Guard uses the Python driver by paazan for the "Contour Next Link 2.4" 
#    radio bridge to the Minimed 670G to download real time data from the pump.
#    https://github.com/pazaan/decoding-contour-next-link
#    
#    For the cloud connection and app communication the official Blynk Python
#    library is used.
#    https://github.com/blynkkk/lib-python
#
#  Author:
#
#    Ondrej Wisniewski (ondrej.wisniewski *at* gmail.com)
#  
#  Changelog:
#
#    23/09/2019 - Initial public release
#    13/10/2019 - Add handling of parameters from configuration file
#    24/10/2019 - Add handling of BGL status codes
#    24/10/2019 - Add handling of display colors according to limits
#    02/11/2019 - Run timer function as asynchronous thread
#    07/11/2019 - Add missing sensor exception codes
#    24/11/2019 - Integrate Nightscout uploader
#    03/12/2019 - Adapt to modified library names
#    03/12/2019 - Make Blynk uploader optional
#    11/02/2020 - Add Blynk virtual pin for active insulin
#    03/03/2020 - Improved robustness of CNL2.4 driver
#    14/04/2020 - Adapt to new data format from CNL driver
#    15/04/2020 - Add more status data to blynk uploader
#    28/06/2020 - Syntax updates for Python3
#    09/11/2020 - Replace Blynk timer with Python timer,
#                 Account for Pump Time drift
#
#  TODO:
#    - Upload missed data when the pump returns into range
#    - Add some notification mechanism for alarms e.g. Telegram or Pushover message
#    - Upload data to Tidepool
#
#  Copyright 2019-2020, Ondrej Wisniewski 
#  
#  This file is part of the DD-Guard project.
#  
#  DD-Guard is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
# 
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
# 
#  You should have received a copy of the GNU General Public License
#  along with crelay.  If not, see <http://www.gnu.org/licenses/>.
#  
###############################################################################

import blynklib
import signal
import syslog
import sys
import time
import threading 
if sys.version_info[0] < 3:
    from ConfigParser import ConfigParser
else:
    from configparser import ConfigParser
import datetime
import cnl24driverlib
import nightscoutlib
from sensor_codes import SENSOR_EXCEPTIONS

VERSION = "0.8"

UPDATE_INTERVAL = 300
RETRY_INTERVAL  = 180
RETRY_DELAY     = 5
MAX_RETRIES_AT_FAILURE = 3

# virtual pin definitions
VPIN_SENSOR  = 1
VPIN_BATTERY = 2
VPIN_UNITS   = 3
VPIN_ARROWS  = 4
VPIN_STATUS  = 5
VPIN_ACTINS  = 6
VPIN_LASTBOLUS = 7

# color definitions
BLYNK_WHITE  = "#F0F0F0"
BLYNK_GREEN  = "#23C48E"
BLYNK_BLUE   = "#04C0F8"
BLYNK_YELLOW = "#ED9D00"
BLYNK_RED    = "#D3435C"
BLYNK_DARK_BLUE = "#5F7CD8"

sensor_exception_codes = {
    SENSOR_EXCEPTIONS.SENSOR_OK:               SENSOR_EXCEPTIONS.SENSOR_OK_STR,
    SENSOR_EXCEPTIONS.SENSOR_INIT:             SENSOR_EXCEPTIONS.SENSOR_INIT_STR,
    SENSOR_EXCEPTIONS.SENSOR_CAL_NEEDED:       SENSOR_EXCEPTIONS.SENSOR_CAL_NEEDED_STR,
    SENSOR_EXCEPTIONS.SENSOR_ERROR:            SENSOR_EXCEPTIONS.SENSOR_ERROR_STR,
    SENSOR_EXCEPTIONS.SENSOR_CAL_ERROR:        SENSOR_EXCEPTIONS.SENSOR_CAL_ERROR_STR,
    SENSOR_EXCEPTIONS.SENSOR_CHANGE_SENSOR:    SENSOR_EXCEPTIONS.SENSOR_CHANGE_SENSOR_STR,
    SENSOR_EXCEPTIONS.SENSOR_END_OF_LIFE:      SENSOR_EXCEPTIONS.SENSOR_END_OF_LIFE_STR,
    SENSOR_EXCEPTIONS.SENSOR_NOT_READY:        SENSOR_EXCEPTIONS.SENSOR_NOT_READY_STR,
    SENSOR_EXCEPTIONS.SENSOR_READING_HIGH:     SENSOR_EXCEPTIONS.SENSOR_READING_HIGH_STR,
    SENSOR_EXCEPTIONS.SENSOR_READING_LOW:      SENSOR_EXCEPTIONS.SENSOR_READING_LOW_STR,
    SENSOR_EXCEPTIONS.SENSOR_CAL_PENDING:      SENSOR_EXCEPTIONS.SENSOR_CAL_PENDING_STR,
    SENSOR_EXCEPTIONS.SENSOR_CHANGE_CAL_ERROR: SENSOR_EXCEPTIONS.SENSOR_CHANGE_CAL_ERROR_STR,
    SENSOR_EXCEPTIONS.SENSOR_TIME_UNKNOWN:     SENSOR_EXCEPTIONS.SENSOR_TIME_UNKNOWN_STR,
    SENSOR_EXCEPTIONS.SENSOR_LOST:             SENSOR_EXCEPTIONS.SENSOR_LOST_STR
}

is_connected = False
lastBolusTime = None
cycleTimer = None
cycleCount = 0

blynk = None
nightscout = None

CONFIG_FILE = "/etc/ddguard.conf"


def to_int(string):
   try:
      i = int(string)
   except:
      i = 0
   return i


#########################################################
#
# Function:    read_config()
# Description: Read parameters from config file
# 
#########################################################
def read_config(cfilename):
   
   # Parameters from global config file
   config = ConfigParser()
   config.read(cfilename)
   
   #TODO: check if file exists

   try:
      # Read Blynk parameters
      read_config.blynk_server    = config.get('blynk', 'server').split("#")[0].strip('"').strip("'").strip()
      read_config.blynk_token     = config.get('blynk', 'token').split("#")[0].strip('"').strip("'").strip()
      read_config.blynk_heartbeat = to_int(config.get('blynk', 'heartbeat').split("#")[0].strip('"').strip("'"))
   except ConfigParser.NoOptionError as NoSectionError:
      syslog.syslog(syslog.LOG_ERR, "ERROR - Needed blynk option not found in config file")
      return False

   try:
      # Read Nightscout parameters
      read_config.nightscout_server     = config.get('nightscout', 'server').split("#")[0].strip('"').strip("'").strip()
      read_config.nightscout_api_secret = config.get('nightscout', 'api_secret').split("#")[0].strip('"').strip("'").strip()
   except ConfigParser.NoOptionError as NoSectionError:
      syslog.syslog(syslog.LOG_ERR, "ERROR - Needed nightscout option not found in config file")
      return False

   try:
      # Read BGL alert parameters
      read_config.bgl_low_val      = to_int(config.get('bgl', 'bgl_low').split("#")[0].strip('"').strip("'"))
      read_config.bgl_pre_low_val  = to_int(config.get('bgl', 'bgl_pre_low').split("#")[0].strip('"').strip("'"))
      read_config.bgl_pre_high_val = to_int(config.get('bgl', 'bgl_pre_high').split("#")[0].strip('"').strip("'"))
      read_config.bgl_high_val     = to_int(config.get('bgl', 'bgl_high').split("#")[0].strip('"').strip("'"))
   except ConfigParser.NoOptionError as NoSectionError:
      syslog.syslog(syslog.LOG_ERR, "ERROR - Needed bgl option not found in config file")
      return False

   # Disable BGL parameters if not specified in config
   if read_config.bgl_pre_high_val == 0:
      read_config.bgl_pre_high_val = 1000
   if read_config.bgl_high_val == 0:
      read_config.bgl_high_val = 1000
      
   print ("Blynk server:    %s" % read_config.blynk_server)
   print ("Blynk token:     %s" % read_config.blynk_token)
   print ("Blynk heartbeat: %d\n" % read_config.blynk_heartbeat)
   print ("Nightscout server:     %s" % read_config.nightscout_server)
   print ("Nightscout api_secret: %s\n" % read_config.nightscout_api_secret)
   print ("BGL low:      %d" % read_config.bgl_low_val)
   print ("BGL pre low:  %d" % read_config.bgl_pre_low_val)
   print ("BGL pre high: %d" % read_config.bgl_pre_high_val)
   print ("BGL high:     %d\n" % read_config.bgl_high_val)
   return True

    
#########################################################
#
# Function:    on_sigterm()
# Description: signal handler for the TERM and INT signal
# 
#########################################################
def on_sigterm(signum, frame):
   try:
      if blynk != None:
         blynk.disconnect()
   except:
      pass
   try:
      if cycleTimer != None:
         cycleTimer.cancel()
   except:
      pass
   syslog.syslog(syslog.LOG_NOTICE, "Exiting DD-Guard daemon")
   sys.exit()


#########################################################
#
# Function:    blynk_upload()
# Description: Blynk uploader
# 
#########################################################
def blynk_upload(data):

   global lastBolusTime
   global cycleCount
   
   if data != None:
      print("Uploading data to Blynk")
       
      # Send sensor data
      if data["sensorBGL"] in sensor_exception_codes:
         # Sensor exception occured
         
         # BGL gauge
         blynk.virtual_write(VPIN_SENSOR, None)
         blynk.set_property(VPIN_SENSOR, "color", BLYNK_WHITE)
         
         # Trend and active insulin
         blynk.virtual_write(VPIN_ARROWS, "--"+" / "+str(data["activeInsulin"]))
         
         # Status line
         blynk.virtual_write(VPIN_STATUS, datetime.datetime.now().strftime("%H:%M")+" - "+sensor_exception_codes[data["sensorBGL"]])
         blynk.set_property(VPIN_STATUS, "color", BLYNK_RED)
      else:
         # Regular BGL data
         
         # BLG gauge
         blynk.virtual_write(VPIN_SENSOR, data["sensorBGL"])
         if data["pumpAlert"]["alertSuspend"] or data["pumpAlert"]["alertSuspendLow"]:
            blynk.set_property(VPIN_SENSOR, "color", BLYNK_BLUE)
         elif data["sensorBGL"] < read_config.bgl_low_val or data["sensorBGL"] > read_config.bgl_high_val or \
              data["pumpAlert"]["alertOnLow"] or data["pumpAlert"]["alertOnHigh"]:
            blynk.set_property(VPIN_SENSOR, "color", BLYNK_RED)
         elif data["sensorBGL"] < read_config.bgl_pre_low_val or data["sensorBGL"] > read_config.bgl_pre_high_val or \
              data["pumpAlert"]["alertBeforeLow"] or data["pumpAlert"]["alertBeforeHigh"]:
            blynk.set_property(VPIN_SENSOR, "color", BLYNK_YELLOW)
         else:
            blynk.set_property(VPIN_SENSOR, "color", BLYNK_GREEN)
         
         # Trend and active insulin
         blynk.virtual_write(VPIN_ARROWS, str(data["trendArrow"])+" / "+str(data["activeInsulin"]))
         
         # Status line
         calTime = "Cal at {0}".format((data["sensorBGLTimestamp"] + datetime.timedelta(minutes=data["sensorCalMinutesRemaining"])).strftime("%H:%M"))
         blynk.virtual_write(VPIN_STATUS, "Updated "+data["sensorBGLTimestamp"].strftime("%H:%M")+" - "+calTime)
         blynk.set_property(VPIN_STATUS, "color", BLYNK_GREEN)
       
      # Send pump data

      # Battery bar
      # Alternate pump and sensor battery
      if cycleCount%2 == 0:
         data_batt = data["batteryLevelPercentage"]
         label_batt = "PUMP BATTERY %"
      else:
         data_batt = data["sensorBatteryLevelPercentage"]
         label_batt = "SENSOR BATTERY %"
      blynk.set_property(VPIN_BATTERY, "label", label_batt)
      blynk.virtual_write(VPIN_BATTERY, data_batt)
      if data_batt <= 25:
         blynk.set_property(VPIN_BATTERY, "color", BLYNK_RED)
      elif data_batt <= 50:
         blynk.set_property(VPIN_BATTERY, "color", BLYNK_YELLOW)
      else:
         blynk.set_property(VPIN_BATTERY, "color", BLYNK_GREEN)
      
      # Reservoir bar
      blynk.virtual_write(VPIN_UNITS, int(round(data["insulinUnitsRemaining"])))
      if data["insulinUnitsRemaining"] <= 25:
         blynk.set_property(VPIN_UNITS, "color", BLYNK_RED)
      elif data["insulinUnitsRemaining"] <= 75:
         blynk.set_property(VPIN_UNITS, "color", BLYNK_YELLOW)
      else:
         blynk.set_property(VPIN_UNITS, "color", BLYNK_GREEN)
         
      # Active insulin / last bolus graph
      if int(data["lastBolusTime"].strftime("%s")) != lastBolusTime: 
         print("Bolus time changed")
         lastBolusTime = int(data["lastBolusTime"].strftime("%s"))
         # Check if last bolus time is recent
         if int(time.time()) - lastBolusTime < 2*UPDATE_INTERVAL:
            print("Bolus time is recent")
            blynk.virtual_write(VPIN_LASTBOLUS, data["lastBolusAmount"])
      else:
         blynk.virtual_write(VPIN_ACTINS, data["activeInsulin"])
      
   else:
      syslog.syslog(syslog.LOG_ERR, "Unable to get data from pump")
      blynk.set_property(VPIN_STATUS, "color", BLYNK_RED)


#########################################################
#
# Function:    upload_live_data()
# Description: Read live data from pump and upload it 
#              to the enabled cloud services
#              This runs once at startup and then as a 
#              periodic timer every 5min
# 
#########################################################
def upload_live_data():
   
   global cycleCount
   global cycleTimer
      
   # Guard against multiple threads
   if upload_live_data.active:
      return
    
   upload_live_data.active = True
   
   print("read live data from pump")
   hasFailed = True
   numRetries = MAX_RETRIES_AT_FAILURE
   while hasFailed and numRetries > 0:
      try:
         liveData = cnl24driverlib.readLiveData()
         hasFailed = False
      except:
         print("unexpected ERROR occured while reading live data")
         syslog.syslog(syslog.LOG_ERR, "Unexpected ERROR occured while reading live data")
         liveData = None
         numRetries -= 1
         if numRetries > 0:
            time.sleep(RETRY_DELAY)
            
   # Account for pump RTC drift
   if liveData != None:
      print("account for pump RTC drift:")
      print("   before: pumpTime {0},  sensorBGLTimestamp {1}".format(liveData["pumpTime"], liveData["sensorBGLTimestamp"]))
      liveData["pumpTime"] += liveData["pumpTimeDrift"]
      if liveData["sensorBGL"] != SENSOR_EXCEPTIONS.SENSOR_LOST:
         liveData["sensorBGLTimestamp"] += liveData["pumpTimeDrift"]
      print("   after : pumpTime {0},  sensorBGLTimestamp {1}".format(liveData["pumpTime"], liveData["sensorBGLTimestamp"]))
    
   # Upload data to Blynk server
   if blynk != None:
      try:
         blynk_upload(liveData)
      except:
         syslog.syslog(syslog.LOG_ERR, "Blynk upload ERROR")

   # Upload data to Nighscout server
   if nightscout != None:
      try:
         nightscout.upload(liveData)
      except:
         syslog.syslog(syslog.LOG_ERR, "Nightscout upload ERROR")
   
   # Calculate time until next reading
   if liveData != None:
      nextReading = liveData["sensorBGLTimestamp"] + datetime.timedelta(seconds=UPDATE_INTERVAL)
      tmoSeconds  = int((nextReading - datetime.datetime.now(liveData["pumpTime"].tzinfo)).total_seconds())
      print("Next reading at {0}, {1} seconds from now\n".format(nextReading,tmoSeconds))
      if tmoSeconds < 0:
         tmoSeconds = RETRY_INTERVAL
   else:
      tmoSeconds = RETRY_INTERVAL
      print("Retry reading {0} seconds from now\n".format(tmoSeconds))
      
   # Start timer for next cycle
   cycleTimer = threading.Timer(tmoSeconds+10, upload_live_data)
   cycleTimer.start()
   
   cycleCount += 1
   upload_live_data.active = False


##########################################################           
# Setup
##########################################################           

# read configuration parameters
if read_config(CONFIG_FILE) == False:
   sys.exit()

blynk_enabled = (read_config.blynk_token != "") and (read_config.blynk_server != "")
nightscout_enabled = (read_config.nightscout_server != "") and (read_config.nightscout_api_secret != "")

# Init Blynk instance
if blynk_enabled:
   print("Blynk upload is enabled")
   blynk = blynklib.Blynk(read_config.blynk_token,
                          server=read_config.blynk_server.strip(),
                          heartbeat=read_config.blynk_heartbeat)

   @blynk.handle_event("connect")
   def connect_handler():
      global is_connected
      if not is_connected:
         is_connected = True
         print("Connected to cloud server")
         syslog.syslog(syslog.LOG_NOTICE, "Connected to cloud server")

   @blynk.handle_event("disconnect")
   def disconnect_handler():
      global is_connected
      if is_connected:
         is_connected = False
         print("Disconnected from cloud server")
         syslog.syslog(syslog.LOG_NOTICE, "Disconnected from cloud server")

# Init Nighscout instance (if requested)
if nightscout_enabled:
   print("Nightscout upload is enabled")
   nightscout = nightscoutlib.nightscout_uploader(server = read_config.nightscout_server, 
                                                  secret = read_config.nightscout_api_secret)


##########################################################           
# Initialization
##########################################################           
syslog.syslog(syslog.LOG_NOTICE, "Starting DD-Guard daemon, version "+VERSION)

# Init signal handler
signal.signal(signal.SIGINT, on_sigterm)
signal.signal(signal.SIGTERM, on_sigterm)

upload_live_data.active = False

# Perform first upload immediately
# Subsequent uploads will be scheduled according to received data timestamp
t = threading.Thread(target=upload_live_data, args=())
t.start()

##########################################################           
# Main loop
##########################################################           
while True:
   if blynk_enabled:
      blynk.run()
   else:
      time.sleep(0.1)
