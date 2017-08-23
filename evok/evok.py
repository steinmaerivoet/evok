#!/usr/bin/python

import os
import ConfigParser
import tornado.httpserver
import tornado.ioloop
import tornado.web
from tornado import gen
from tornado.options import define, options
from tornado import websocket
from tornado import escape
from tornado.concurrent import is_future

from tornado.process import Subprocess  # not sure about it
import subprocess  # not sure about it

from log import *
from tornado_json.api_doc_gen import get_api_docs
#from test.badsyntax_future3 import result

try:
	from urllib.parse import urlparse  # py2
except ImportError:
	from urlparse import urlparse  # py3

import signal
import rpc_handler
import json
import config
from devices import *

from tornado_json.requesthandlers import APIHandler
from tornado_json import schema, api_doc_gen

Config = config.EvokConfig() #ConfigParser.RawConfigParser()
cors = False
corsdomains = '*'

class UserCookieHelper():
	_passwords = []

	def get_current_user(self):
		if len(self._passwords) == 0: return True
		return self.get_secure_cookie("user")


def enable_cors(handler):
	if cors:
		handler.set_header("Access-Control-Allow-Headers", "*")
		handler.set_header("Access-Control-Allow-Headers", "Content-Type, Depth, User-Agent, X-File-Size,"
														   "X-Requested-With, X-Requested-By, If-Modified-Since, X-File-Name, Cache-Control")
		handler.set_header("Access-Control-Allow-Origin", corsdomains)
		handler.set_header("Access-Control-Allow-Credentials", "true")
		handler.set_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")



class IndexHandler(UserCookieHelper, tornado.web.RequestHandler):

	def initialize(self, staticfiles):
		self.index = '%s/index.html' % staticfiles
		enable_cors(self)

	@tornado.web.authenticated
	@tornado.gen.coroutine
	def get(self):
		self.render(self.index)


registered_ws = {}

class WsHandler(websocket.WebSocketHandler):

	def check_origin(self, origin):
		# fix issue when Node-RED removes the 'prefix://'
		origin_origin = origin
		parsed_origin = urlparse(origin)
		origin = parsed_origin.netloc
		origin = origin.lower()
		host = self.request.headers.get("Host")
		'''if config.cors:
			domains = config.corsdomains.split()
			if origin in domains or origin_origin in domains:
				return True
		'''
		#return origin == host or origin_origin == host
		return True
		
	def open(self):
		logger.debug("New WebSocket client connected")
		if not registered_ws.has_key("all"):
			registered_ws["all"] = set()

		registered_ws["all"].add(self)

	def on_event(self, device):
		#print "Sending to: %s,%s" % (str(self), device)
		try:
			print device.full()
			self.write_message(json.dumps(device.full()))
		except Exception as e:
			logger.error("Exc: %s", str(e))
			pass

	@tornado.gen.coroutine
	def on_message(self, message):
		try:
			message = json.loads(message)
			try:
				cmd = message["cmd"]
			except:
				cmd = None
			#get FULL state of each IO
			if cmd == "all":
				result = []
				devices = [INPUT, RELAY, AI, AO, SENSOR]
				for dev in devices:
					result += map(lambda dev: dev.full(), Devices.by_int(dev))
				self.write_message(json.dumps(result))
			#set device state
			elif cmd is not None:
				dev = message["dev"]
				circuit = message["circuit"]
				try:
					value = message["value"]
				except:
					value = None
				try:
					device = Devices.by_name(dev, circuit)
					# result = device.set(value)
					func = getattr(device, cmd)
					#print type(value), value
					if value is not None:
						if type(value) == dict:
							result = func(**value)
						else:
							result = func(value)
					else:
						result = func()
					if is_future(result):
						result = yield result
					#send response only to the client requesting full info
					if cmd == "full":
						self.write_message(result)
				#nebo except Exception as e:
				except Exception, E:
					logger.error("Exc: %s", str(E))
					#self.write_message({"error_msg":"Couldn't process this request"})

		except:
			logger.debug("Skipping WS message: %s", message)
			# skip it since we do not understand this message....
			pass

	def on_close(self):
		if registered_ws.has_key("all") and (self in registered_ws["all"]):
			registered_ws["all"].remove(self)
			if len(registered_ws["all"]) == 0:
				for neuron in Devices.by_int(NEURON):
					neuron.stop_scanning()
			#elif registered_ws.has_key("nfc") and (registered_ws["nfc"] == self):
			#	registered_ws["nfc"] = None


class LogoutHandler(tornado.web.RequestHandler):	# ToDo CHECK
	def get(self):
		self.clear_cookie("user")
		self.redirect(self.get_argument("next", "/"))


class LoginHandler(tornado.web.RequestHandler):
	def initialize(self):
		enable_cors(self)

	def post(self):   # ToDo CHECK
		#username = self.get_argument("username", "")
		username = 'admin'
		password = self.get_argument("password", "")
		auth = self.check_permission(password, username)
		if auth:
			self.set_secure_cookie("user", escape.json_encode(username))
			self.redirect(self.get_argument("next", u"/"))
		else:
			error_msg = u"?error=" + tornado.escape.url_escape("Login incorrect")
			self.redirect(u"/auth/login/" + error_msg)

	def get(self):	# ToDo CHECK
		self.redirect(self.get_argument("next", u"/"))
		# self.render("index.html", next=self.get_argument("next","/"))
		# try:
		#	 errormessage = self.get_argument("error")
		# except:
		#	 errormessage = ""
		# #TODO: vymyslet jak udelat login popup
		# #self.render("login.html", errormessage = errormessage)
		# self.write('<html><body><div>%s</div><form action="" method="post">'
		#			'Password: <input type="text" name="name">'
		#			'<input type="submit" value="Sign in">'
		#			'</form></body></html>' % errormessage)

	def check_permission(self, password, username=''):
		if username == "admin" and password in self._passwords:
			return True
		return False


class RestHandler(UserCookieHelper, tornado.web.RequestHandler):
	def initialize(self):
		enable_cors(self)
		self.set_header("Access-Control-Allow-Origin", "*")
		self.set_header("Access-Control-Allow-Headers", "x-requested-with")
		self.set_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')

	# usage: GET /rest/DEVICE/CIRCUIT
	#		or
	#		GET /rest/DEVICE/CIRCUIT/PROPERTY

	@tornado.web.authenticated
	def get(self, dev, circuit, prop):
		#print "%s-%s-%s" %(dev,circuit,prop)
		device = Devices.by_name(dev, circuit)
		if prop:
			if prop[0] in ('_'): raise Exception('Invalid property name')
			result = {prop: getattr(device, prop)}
		else:
			result = device.full()
		self.write(json.dumps(result))
		self.finish()


	# usage: POST /rest/DEVICE/CIRCUIT
	#		  post-data: prop1=value1&prop2=value2...

	#@tornado.web.authenticated
	@tornado.gen.coroutine
	def post(self, dev, circuit, prop):
		try:
			#print "%s-%s-%s" %(dev,circuit,prop)
			device = Devices.by_name(dev, circuit)
			kw = dict([(k, v[0]) for (k, v) in self.request.body_arguments.iteritems()])
			result = device.set(**kw)
			if is_future(result):
				result = yield result
			#print result
			self.write(json.dumps({'success': True, 'result': result}))
		except Exception, E:
			self.write(json.dumps({'success': False, 'errors': {'__all__': str(E)}}))
		self.finish()
	
	
	def options(self):
		# no body
		self.set_status(204)
		self.finish()


class RemoteCMDHandler(UserCookieHelper, tornado.web.RequestHandler): # ToDo CHECK
	def initialize(self):
		enable_cors(self)

	@tornado.gen.coroutine
	@tornado.web.authenticated
	def post(self):
		service = self.get_argument('service', '')
		status = self.get_argument('status', '')
		if service in ('ssh', 'sshd'):
			if status in ('start', 'stop', 'enable', 'disable'):
				result, error = yield call_shell_subprocess('service %s %s' % (service, status))
		if service == 'pw':
			#print 'echo -e "%s\n%s" | passwd root' % (status, status)
			yield call_shell_subprocess('echo -e "%s\\n%s" | passwd root' % (status, status))
		self.finish()

class ConfigHandler(UserCookieHelper, tornado.web.RequestHandler): # ToDo CHECK
	def initialize(self):
		enable_cors(self)

	@tornado.web.authenticated
	def get(self):
		self.write(Config.configtojson())
		self.finish()

	@tornado.gen.coroutine
	@tornado.web.authenticated
	def post(self):
		conf = ConfigParser.ConfigParser()
		#make sure it it saved in the received order
		from collections import OrderedDict
		data = json.loads(self.request.body, object_pairs_hook=OrderedDict)
		for key in data:
			conf.add_section(key)
			for param in data[key]:
				val = data[key][param]
				conf.set(key, param, val)
		cfgfile = open(config.config_path, 'w')
		conf.write(cfgfile)
		cfgfile.close()
		#and call restart
		#TODO: fix systemctl in debian 9?
		yield call_shell_subprocess('service evok restart')
		self.finish()


@gen.coroutine
def call_shell_subprocess(cmd, stdin_data=None, stdin_async=False):
	"""
	Wrapper around subprocess call using Tornado's Subprocess class.
	"""
	stdin = Subprocess.STREAM if stdin_async else subprocess.PIPE

	sub_process = tornado.process.Subprocess(
		cmd, stdin=stdin, stdout=Subprocess.STREAM, stderr=Subprocess.STREAM, shell=True
	)

	if stdin_data:
		if stdin_async:
			yield Subprocess.Task(sub_process.stdin.write, stdin_data)
		else:
			sub_process.stdin.write(stdin_data)

	if stdin_async or stdin_data:
		sub_process.stdin.close()

	result, error = yield [
		gen.Task(sub_process.stdout.read_until_close),
		gen.Task(sub_process.stderr.read_until_close)
	]

	raise gen.Return((result, error))

class LoadAllHandler(UserCookieHelper, APIHandler):
	def initialize(self):
		enable_cors(self)
		self.set_header("Access-Control-Allow-Origin", "*")
		self.set_header("Access-Control-Allow-Headers", "x-requested-with")
		self.set_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')

	#@tornado.gen.coroutine
	#@tornado.web.authenticated
	@schema.validate(output_schema = {
									"$schema": "http://json-schema.org/draft-04/schema#",
									"title": "Neuron_Instruction",
									"type": "array",
									"items": {
											"anyOf": [
												{
													"type": "object",
													"properties": {
														"dev": {
															"type": "string",
															"enum": ["input"]
														},
														"circuit": {"type": "string"},
														"value": {"type": "number"},
														"counter": {"type": "number"},
														"counter_mode": {
															"type": "string",
															"enum": ["disabled"]
														},
														"debounce": {"type": "number"}
													},
													"required": ["dev", "circuit", "value", "counter", "counter_mode", "debounce"]
												},
												{
													"type": "object",
													"properties": {
														"dev": {
															"type": "string",
															"enum": ["relay"]
														},
														"circuit": {"type": "string"},
														"value": {"type": "number"},
														"pending": {"type": "boolean"}
													},
													"required": ["dev", "circuit", "value", "pending"]
												},
												{
													"type": "object",
													"properties": {
														"dev": {
															"type": "string",
															"enum": ["ai"]
														},
														"circuit": {"type": "string"},
														"unit": {"type": "string"},
														"value": {"type": "number"}
													},
													"required": ["dev", "circuit", "unit", "value"]
												},
												{
													"type": "object",
													"properties": {
														"dev": {
															"type": "string",
															"enum": ["ao"]
														},
														"circuit": {"type": "string"},
														"unit": {"type": "string"},
														"value": {"type": "number"}
													},
													"required": ["dev", "circuit", "unit", "value"]
												}
											]
										},
									},
					 output_example = [{"circuit": "1_01", "debounce": 50, "counter": 0, "value": 0, "dev": "input", "counter_mode": "disabled"},
									   {"circuit": "1_02", "debounce": 50, "counter": 0, "value": 0, "dev": "input", "counter_mode": "disabled"},
									   {"circuit": "1_03", "debounce": 50, "counter": 0, "value": 0, "dev": "input", "counter_mode": "disabled"},
									   {"circuit": "1_04", "debounce": 50, "counter": 0, "value": 0, "dev": "input", "counter_mode": "disabled"},
									   {"value": 0, "pending": False, "circuit": "1_01", "dev": "relay"},
									   {"value": 0, "pending": False, "circuit": "1_02", "dev": "relay"},
									   {"value": 0, "pending": False, "circuit": "1_03", "dev": "relay"},
									   {"value": 0, "pending": False, "circuit": "1_04", "dev": "relay"},
									   {"value": 0.004243475302661791, "unit": "V", "circuit": "1_01", "dev": "ai"},
									   {"value": 0.006859985867523581, "unit": "V", "circuit": "1_02", "dev": "ai"},
									   {"value": -0.0001, "unit": "V", "circuit": "1_01", "dev": "ao"}])
	def get(self):
		"""aaa"""
		print Devices.by_int(INPUT)
		result = map(lambda dev: dev.full(), Devices.by_int(INPUT))
		result += map(lambda dev: dev.full(), Devices.by_int(RELAY))
		result += map(lambda dev: dev.full(), Devices.by_int(AI))
		result += map(lambda dev: dev.full(), Devices.by_int(AO))
		result += map(lambda dev: dev.full(), Devices.by_int(SENSOR))
		#self.write(json.dumps(result))
		return result
	
	def options(self):
		# no body
		self.set_status(204)
		self.finish()
		
class JSONHandler(APIHandler):
		def initialize(self):
			#enable_cors(self)
			self.set_header("Content-Type", "application/json")
			self.set_header("Access-Control-Allow-Origin", "*")
			self.set_header("Access-Control-Allow-Headers", "x-requested-with")
			self.set_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
			
		
		def options(self):
			# no body
			self.set_status(204)
			self.finish()
		
		@schema.validate(
			input_schema={
				"$schema": "http://json-schema.org/draft-04/schema#",
				"title": "Neuron_Instruction",
				"type": "object",
				"properties": {
					"commands": {
						"type": "array",
						"items": {
							"type": "object",
							"properties": {
								"id": {
									"type": "number"
								}, 
								"dev_type": {
									"type": "number"
								},
								"command": {
									"type": "object",
									"oneOf": [
										{
											"type": "object",
											"properties": {
												"command_name": {
													"type": "string",
													"enum": ["probe"]
												}
											},
											"required": ["command_name"]
										},
										{
											"type": "object",
											"properties": {
												"command_name": {
													"type": "string",
													"enum": ["set"]
												},
												"field": {
													"type": "string",
													"enum": ["Register", "DO", "DI", "AO", "AI", "RO"]
												},
												"field_index": {
													"type": "number"
												},
												"value": {
													"type": "number",
												}
											},
											"required": ["command_name", "field", "value"]										
										},
										{
											"type": "object",
											"properties": {
												"command_name": {
													"type": "string",
													"enum": ["send"]
												},
												"command_data": {
													"type": "array",
													"items": {
														"type": "number"
													}
												}
											},
											"required": ["command_name", "command_data"]										
										}
									]
								},
													
							},
							"required": ["id", "dev_type", "command"]
						},
						"minItems": 1,
						"uniqueItems": True 
					},
					"queries": {
						"type": "array",
						"items": {
							"type": "object",
							"properties": {
								"dev_type": {
									"type": "string"
								},
								"dev_id": {
									"type": "number"
								},
								"field": {
									"type": "string",
									"enum": ["Name", "Features", "SWVersion", "HWVersion", "Register", "DO", "DI", "AO", "AI", "RO"]
								},
								"field_index": {
									"type": "number"
								}
							},
							"required": ["dev_id", "dev_type", "field"]
						},							
						"minItems": 1,
						"uniqueItems": True 
					},
					"probe_all": {
						"type": "boolean",
						"enum": [True]
					}
				}
						
			},
			input_example={
							
			},
			output_schema={
				"$schema": "http://json-schema.org/draft-04/schema#",
				"title": "Neuron_Reply",
				"type": "object",
				"properties": {
					"commands": {
						"type": "array",
						"items": {
							"type": "object",
							"properties": {
								"id": {
									"type": "number"
								}, 
								"dev_type": {
									"type": "number"
								},
								"command": {
									"type": "object",
									"oneOf": [
										{
											"type": "object",
											"properties": {
												"command_name": {
													"type": "string",
													"enum": ["probe"]
												}
											},
											"required": ["command_name"]
										},
										{
											"type": "object",
											"properties": {
												"command_name": {
													"type": "string",
													"enum": ["set"]
												},
												"field": {
													"type": "string",
													"enum": ["Register", "DO", "DI", "AO", "AI", "RO"]
												},
												"field_index": {
													"type": "number"
												},
												"value": {
													"type": "number",
												}												
											},
											"required": ["command_name", "field", "value"]										
										},
										{
											"type": "object",
											"properties": {
												"command_name": {
													"type": "string",
													"enum": ["send"]
												},
												"command_data": {
													"type": "array",
													"items": {
														"type": "number"
													}
												},
												"address": {
													"type": "number"
												}												
											},
											"required": ["command_name", "command_data"]										
										}
									]
								},
								"performed": {
									"type": "boolean"
								}					
							},
							"required": ["id", "dev_type", "command", "performed"]
						},
						"minItems": 1,
						"uniqueItems": True 
					},
					"queries": {
						"type": "array",
						"items": {
							"type": "object",
							"oneOf": [
								{
									"properties": {
										"dev_type": {
											"type": "string"
										},
										"dev_id": {
											"type": "number"
										},
										"field": {
											"type": "string",
											"enum": ["Name", "Features", "SWVersion", "HWVersion"]
										},
										"field_index": {
											"type": "number"
										},
										"performed": {
											"type": "boolean"
										},
										"reply": {
											"type": "string"
										}
									},
									"required": ["dev_id", "dev_type", "field", "performed"]
								},
								{
									"properties": {
										"dev_type": {
											"type": "string"
										},
										"dev_id": {
											"type": "number"
										},
										"field": {
											"type": "string",
											"enum": ["Register", "Sensor", "DO", "DI", "AO", "AI", "RO"]
										},
										"field_index": {
											"type": "number"
										},
										"performed": {
											"type": "boolean"
										},
										"reply": {
											"type": "number"
										}
									},
									"required": ["dev_id", "dev_type", "field", "performed"]
								},
								{
									"properties": {
										"dev_type": {
											"type": "string"
										},
										"dev_id": {
											"type": "number"
										},
										"field": {
											"type": "string",
											"enum": ["Channel"]
										},
										"field_index": {
											"type": "number"
										},
										"performed": {
											"type": "boolean"
										},
										"reply": {
											"type": "string"
										}
									},
									"required": ["dev_id", "dev_type", "field", "performed"]									
								}
							]
						},							
						"minItems": 1,
						"uniqueItems": True 
					},
					"probe_all": {
						"type": "array",
						"items": {
							"type": "object",
							"properties": {
								"dev_type": {
									"type": "string"
								},
								"dev_id": {
									"type": "number"
								},
								"Name": {
									"type": "string"
								},
								"SWVersion": {
									"type": "string"
								},
								"HWVersion": {
									"type": "string"
								},
								"features": {
									"type": "object",
									"oneOf": [
												{
													"properties": {
														"field": {
															"type": "string",
															"enum": ["DO"]
														},
														"index_minor": {
															"type": "number"
														},
														"index_major": {
															"type": "number"
														},
														"min_v": {
															"type": "number"
														},
														"max_v": {
															"type": "number"
														}		
													}
												},
												{
													"properties": {
														"field": {
															"type": "string",
															"enum": ["DI"]
														},
														"index_minor": {
															"type": "number"
														},
														"index_major": {
															"type": "number"
														},
														"trig_v_min": {
															"type": "number"
														},
														"trig_v_max": {
															"type": "number"
														},
														"max_v": {
															"type": "number"
														}
													}
												},
												{
													"properties": {
														"field": {
															"type": "string",
															"enum": ["AO"]
														},
														"index_minor": {
															"type": "number"
														},
														"index_major": {
															"type": "number"
														},
														"modes": {
															"type": "array",
															"items": {
																"type": "string",
																"enum": ["Voltage", "Current"]
															}
														},
														"min_v": {
															"type": "number"
														},
														"max_v": {
															"type": "number"
														},
														"min_a": {
															"type": "number"
														},
														"max_a": {
															"type": "number"
														}
													},
												},
												{
													"properties": {
														"field": {
															"type": "string",
															"enum": ["AI"]
														},
														"index_minor": {
															"type": "number"
														},
														"index_major": {
															"type": "number"
														},
														"modes": {
															"type": "array",
															"items": {
																"type": "string",
																"enum": ["Voltage, Current"]
															}
														},
														"min_v": {
															"type": "number"
														},
														"max_v": {
															"type": "number"
														},
														"min_a": {
															"type": "number"
														},
														"max_a": {
															"type": "number"
														}
													}
												},
												{
													"properties": {
														"field": {
															"type": "string",
															"enum": ["RO"]
														},
														"index_minor": {
															"type": "number"
														},
														"index_major": {
															"type": "number"
														},
														"modes": {
															"type": "array",
															"items": {
																"type": "string",
																"enum": ["Simple", "Counter", "DirectSwitch"]
															}
														},
														"max_v": {
															"type": "number"
														},
														"max_a": {
															"type": "number"
														}
													},
												},
												{
													"properties": {
														"field": {
															"type": "string",
															"enum": ["Sensor"]
														},
														"index_minor": {
															"type": "number"
														},
														"index_major": {
															"type": "number"
														},
														"val_name": {
															"type": "string"
														},
														"min_val": {
															"type": "number"
														},
														"max_val": {
															"type": "number"
														}	
													}
												},
												{	
													"properties": {
														"field": {
															"type": "string",
															"enum": ["Register"]
														},
														"start": {
															"type": "number"
														},
														"end": {
															"type": "number"
														},
														"writable": {
															"type": "boolean"
														},
														"index_major": {
															"type": "number"
														}
													}
												},
												{	
													"properties": {
														"field": {
															"type": "string",
															"enum": ["Channel"]
														},
														"protocol": {
															"type": "string",
															"enum": ["I2C", "DALI", "SPI", "RS485", "1WIRE"]
														}
													}
												}
											]
										}
									}
								}		
							
						}
					}
				
			},
			output_example={
				
			},
		)
		def post(self):
			return {"message":"abcd"}
		
# callback generators for devents
def gener_status_cb(mainloop, modbus_context):

	def status_cb_modbus(device, *kwargs):
		modbus_context.status_callback(device)
		if registered_ws.has_key("all"):
			map(lambda x: x.on_event(device), registered_ws['all'])
		pass

	def status_cb(device, *kwargs):
		#if add_computes(device):
		#	mainloop.add_callback(compute)
		if registered_ws.has_key("all"):
			map(lambda x: x.on_event(device), registered_ws['all'])
		pass

	if modbus_context:
		return status_cb_modbus
	return status_cb


def gener_config_cb(mainloop, modbus_context):

	def config_cb_modbus(device, *kwargs):
		modbus_context.config_callback(device)

	def config_cb(device, *kwargs):
		pass
		# if registered_ws.has_key("all"):
		#	 map(lambda x: x.on_event(device), registered_ws['all'])
		#if add_computes(device):
		#	mainloop.add_callback(compute)
			# print device
			# d = device.full()
			# print "%s%s " % (d['dev'], d['circuit'])

	if modbus_context:
		return config_cb_modbus
	return config_cb


################################ MAIN ################################

def main():

	# define("path1", default='', help="Use this config file, if device is Unipi 1.x", type=str)
	# define("path2", default='', help="Use this config file, if device is Unipi Neuron", type=str)
	define("port", default=-1, help="Http server listening ports", type=int)
	define("modbus_port", default=-1, help="Modbus/TCP listening port, 0 disables modbus", type=int)
	tornado.options.parse_command_line()

	config.read_eprom_config()

	Config.add_section('MAIN')
	path = '/etc/evok.conf'
	# set config file name based on eprom version and command line option --path1 --path2
	# if config.globals['version1']:
	#	 path1 = options.as_dict()['path1']
	#	 if (path1 != '') and (os.path.isfile(path1)):
	#		 path = path1
	# elif config.globals['version2']:
	#	 path2 = options.as_dict()['path2']
	#	 if (path2 != '') and (os.path.isfile(path2)):
	#		 path = path2

	if not os.path.isfile(path):
		path = os.path.dirname(os.path.realpath(__file__)) + '/evok.conf'

	Config.read(path)
	log_file = Config.getstringdef("MAIN", "log_file", "/var/log/evok.log")
	log_level = Config.getstringdef("MAIN", "log_level", "ERROR").upper()

	#rotating file handler
	filelog_handler = logging.handlers.TimedRotatingFileHandler(filename=log_file, when='D', backupCount=7)
	log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
	filelog_handler.setFormatter(log_formatter)
	filelog_handler.setLevel(log_level)
	logger.addHandler(filelog_handler)

	logger.info("Starting using config file %s", path)

	webname = Config.getstringdef("MAIN", "webname", "unipi")
	staticfiles = Config.getstringdef("MAIN", "staticfiles", "/var/www/evok")
	cookie_secret = Config.getstringdef("MAIN", "secret", "ut5kB3hhf6VmZCujXGQ5ZHb1EAfiXHcy")
	hw_dict = config.HWDict('./etc/hw_definitions/')
	pw = Config.getstringdef("MAIN", "password", "")
	if pw: userCookieHelper._passwords.append(pw)
	pw = Config.getstringdef("MAIN", "rpcpassword", "")
	if pw: rpc_handler.userBasicHelper._passwords.append(pw)

	cors = Config.getbooldef("MAIN", "enable_cors", False)
	corsdomains = Config.getstringdef("MAIN", "cors_domains", "*")
	define("cors", default=True, help="enable CORS support", type=bool)
	#define("corsdomains", default=corsdomains, help="CORS domains separated by whitespace", type=bool)
	port = 8080
	#port = Config.getintdef("MAIN", "port", 8080)
	#if options.as_dict()['port'] != -1:
	#	port = options.as_dict()['port'] # use command-line option instead of config option

	modbus_address = Config.getstringdef("MAIN", "modbus_address", '')
	modbus_port = Config.getintdef("MAIN", "modbus_port", 0)
	if options.as_dict()['modbus_port'] != -1:
		modbus_port = options.as_dict()['modbus_port'] # use command-line option instead of config option

	app_routes = [
			#(r"/", web.RedirectHandler, {"url": "http://%s/" % webname }),
			(r"/auth/login/", LoginHandler),
			(r"/auth/logout/", LogoutHandler),
			(r"/rpc", rpc_handler.Handler),
			(r"/config", ConfigHandler),
			(r"/config/cmd", RemoteCMDHandler),
			(r"/json", JSONHandler),
			(r"/rest/all/?", LoadAllHandler),
			(r"/rest/([^/]+)/([^/]+)/?([^/]+)?/?", RestHandler),
			(r"/ws", WsHandler),
			]
	
	
	app = tornado.web.Application(
		handlers=app_routes,
		login_url='/auth/login/',
		cookie_secret=cookie_secret
	)
	docs = get_api_docs(app_routes)
	print docs
	try:
		with open('./API_docs.md', "w") as api_out:
			api_out.writelines(docs)
	except Exception, e:
		pass


	#app.add_handlers(r'%s.*' % webname , [(r"/", IndexHandler, dict(staticfiles=staticfiles))])


	#### prepare http server #####
	httpServer = tornado.httpserver.HTTPServer(app)
	httpServer.listen(port)
	logger.info("HTTP server listening on port: %d", port)
	
	if modbus_port > 0: # used for UniPi 1.x
		from modbus_tornado import ModbusServer, ModbusApplication
		import modbus_unipi
		#modbus_context = modbus_unipi.UnipiContext()  # full version
		modbus_context = modbus_unipi.UnipiContextGpio()  # limited version

		modbus_server = ModbusServer(ModbusApplication(store=modbus_context, identity=modbus_unipi.identity))
		modbus_server.listen(modbus_port, address=modbus_address)
		logger.info("Modbus/TCP server istening on port: %d", modbus_port)
	else:
		modbus_context = None

	mainLoop = tornado.ioloop.IOLoop.instance()

	#### prepare hardware according to config #####
	# prepare callbacks for config events
	devents.register_config_cb(gener_config_cb(mainLoop, modbus_context))
	devents.register_status_cb(gener_status_cb(mainLoop, modbus_context))
	# create hw devices
	config.create_devices(Config)
	'''
	""" Setting the '_server' attribute if not set - simple link to mainloop"""
	for (srv, urlspecs) in app.handlers:
		for urlspec in urlspecs:
			try:
				setattr(urlspec.handler_class, '_server', mainLoop)
			except AttributeError:
				urlspec.handler_class._server = mainLoop
	'''
	# switch buses to async mode, start processes, plan some actions
	for bustype in (I2CBUS, GPIOBUS, ADCHIP, OWBUS, NEURON):
		for device in Devices.by_int(bustype):
			device.switch_to_async(mainLoop)

	for neuron in Devices.by_int(NEURON):
		if neuron.scan_enabled:
			neuron.start_scanning()

	def sig_handler(sig, frame):
		if sig in (signal.SIGTERM, signal.SIGINT):
			tornado.ioloop.IOLoop.instance().add_callback(shutdown)

	#gracefull shutdown
	def shutdown():
		for bus in Devices.by_int(I2CBUS):
			bus.switch_to_sync()
		for bus in Devices.by_int(GPIOBUS):
			bus.switch_to_sync()
		logger.info("Shutting down")
		#try: httpServer.stop()
		#except: pass
		#todo: and shut immediately?
		tornado.ioloop.IOLoop.instance().stop()

	signal.signal(signal.SIGTERM, sig_handler)
	signal.signal(signal.SIGINT, sig_handler)

	mainLoop.start()


if __name__ == "__main__":
	main()
