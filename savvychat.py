from google.appengine.api import channel
from google.appengine.api import users
from google.appengine.api import memcache
from google.appengine.api import mail

from google.appengine.ext import webapp
from google.appengine.ext import db
from google.appengine.ext.webapp.util import run_wsgi_app
from google.appengine.ext.webapp import template

from django.utils import simplejson

from datetime import datetime
import time

from random import choice, randint

from sets import Set

import urllib
import urlparse

import re

import traceback

MAXPOSTS = 30 #max number of posts initialized
EXTRAPOSTS = 20 #number of posts received when "view older" is checked (should be < MAXPOSTS)
TOKENRECYCLEAGE = 2*60*60-5*60 #if a token is older than this, we won't give it to a new user (2 hours is token lifetime, then minus 5 mins).
TOKENREMOVEAGE = 2*60*60+5*60 #if a token is older than this, we'll consider it dead.
HOUSEKEEPINGINTERVAL = 5*60
AUTODUMPINTERVAL = 24*60*60

#http://stackoverflow.com/questions/2350454/simplest-way-to-store-a-value-in-google-app-engine-python
class Global(db.Model):
	#store miscellaneous strings
	value = db.StringProperty()
	values = db.StringListProperty()

	@classmethod
	def get(cls, key):
		value = memcache.get(key)
		if not value:
			instance = cls.get_by_key_name(key)
			if instance:
				value = instance.value or instance.values
				memcache.set(key=key, value=value)
			else:
				return None
		return value

	@classmethod
	def set(cls, key, value):
		memcache.set(key=key, value=value)
		entity = cls(key_name=key)
		if type(value) is list: entity.values = value
		else: entity.value = value
		entity.put()
		return value

class Chatchannel(db.Model):
	#store miscellaneous strings
	birthday = db.DateTimeProperty(auto_now_add=True)
	token = db.StringProperty()
	owner = db.StringProperty()

class Post(db.Model):
	#each post stored in the DB is of this class
	author = db.StringProperty()
	content = db.TextProperty()#if i use string, there's a 500 character limit
	recipients = db.StringListProperty()#for call
	pendingemails = db.ListProperty(bool)
	pendingemailsq = db.BooleanProperty(default=False)
	date = db.DateTimeProperty(auto_now_add=True)

class Chatuser(db.Model):
	#this user data is automatically created on first logon
	userid = db.StringProperty()
	name = db.StringProperty()
	email = db.EmailProperty()
	playtone = db.BooleanProperty(default=False)
	shiftsend = db.BooleanProperty(default=True)
	hf = db.BooleanProperty(default=True)
	lastonline = db.DateTimeProperty()
	
class File(db.Model):
	#for file upload
	type = db.StringProperty()
	name = db.StringProperty()
	data = db.BlobProperty()
	date = db.DateTimeProperty(auto_now_add=True)

class Whiteuser(db.Model):
	#whitelisted users
	email = db.EmailProperty()
	name = db.StringProperty()

class Alias(db.Model):
	#whitelisted users
	aliastext = db.StringProperty()
	meaning = db.StringProperty()

def getFreeChannel(userid):
	tokenids = fetchTokens()
	tokenswriteflag = False #did we make changes
	chatchannel = None
	for tokenid in tokenids:
		chatchannel = Chatchannel.get_by_key_name(tokenid)
		age = TOKENREMOVEAGE+1
		if chatchannel:
			age = (datetime.now()-chatchannel.birthday).seconds
		if age < TOKENRECYCLEAGE and not chatchannel.owner:
			chatchannel.owner = userid
			chatchannel.put()
			break
		if age > TOKENREMOVEAGE or not chatchannel.owner:
			if chatchannel: chatchannel.delete()
			tokenids = tokenids[1:]
			tokenswriteflag = True
		chatchannel = None
	if not chatchannel:
		chatchannel = createChannel(userid,tokenids)
		tokenswriteflag = True
	if tokenswriteflag: Global.set("tokens", tokenids)
	return chatchannel

def createChannel(userid,tokenids):
	tokenid = randint(0,50)
	while tokenid in tokenids: tokenid += 1
	chatchannel = Chatchannel(key_name=str(tokenid))
	chatchannel.token = channel.create_channel(str(tokenid))
	chatchannel.owner = userid
	chatchannel.put()
	tokenids += [str(tokenid)]
	return chatchannel

def fetchTokens():
	return Global.get("tokens") or []

def delToken(chatuser, idx):
	try:
		del chatuser.lastrefreshlist[idx]
	except IndexError: pass
	try:
		del chatuser.tokens[idx]
	except IndexError: pass

def broadcast(post,dontSave=False):
	#send a message to everyone who is online
	tokens = fetchTokens()
	message = simplejson.dumps(makePostObject(post,dontSave))
	for token in tokens:
		channel.send_message(token, message)

def sendMail(to, subject, body, html=None, senderName='SavvyChat Mailer', senderMail='mailer'):
	netloc = getNetloc()
	if not netloc:
		doPost("not configured to send emails!","SavvyChat",dontSave=True)
		return
	netlocData = netloc.split('.')
	if len(netlocData) > 2 and netlocData[-2]=="appspot":
		hostname = netlocData[-3]
		params = {"sender":senderName+" <"+senderMail+"@"+hostname+".appspotmail.com>",
			"to":to,
			"subject":subject,
			"body":body}
		if html is not None:
			params["html"] = html
		mail.send_mail(**params)
		
def adjustPendingEmails(post):
	post.pendingemailsq = True in post.pendingemails

def call(post,force=False):
	if len(post.recipients) == 0:
		return
	#send out emails
	plaincontent = htmlcontent = post.content
	TOarray = []
	for recipient in post.recipients:
		chatuser = getUserFromName(recipient.title())
		if chatuser and (force or userOnline(chatuser.userid)):
			idx = post.recipients.index(chatuser.name.lower())
			if post.pendingemails[idx]:
				TOarray = TOarray + [chatuser.name + ' <' + chatuser.email + '>']
				post.pendingemails[idx]=False
	adjustPendingEmails(post)
	post.put()
	if TOarray != []:
		sendMail(to=', '.join(TOarray),
			subject=getUserFromId(post.author).name+" sent you a message with SavvyChat",
			body=plaincontent,
			html=htmlcontent)

def removeWhitespace(data):
	return ''.join(data.split())
		
def housekeeping():
	lastHK = Global.get("lastHK")
	if not lastHK: lastHK = "1"
	lastHKInt = int(lastHK)
	nowInt = int(time.mktime(datetime.now().timetuple()))
	if nowInt-lastHKInt < HOUSEKEEPINGINTERVAL:
		return

	resolvePendingEmails()
	checkDump()

	Global.set("lastHK", str(nowInt))

def resolvePendingEmails():
	pendingposts = db.GqlQuery("SELECT * FROM Post WHERE pendingemailsq=TRUE")
	for post in pendingposts:
		#if (datetime.now()-post.date).seconds > 60:
		call(post,True)
	pass

def getLastDump():
	try:
		startInt = int(Global.get("lastDump"))
	except TypeError:
		firstPost = db.GqlQuery("SELECT * FROM Post ORDER BY date ASC").get()
		if firstPost is None:
			return 0
		else:
			startInt = int(time.mktime(firstPost.date.timetuple()))
	return startInt
		
def checkDump():
	#check if its been about a day since the last dump
	#get start time
	startInt = getLastDump()

	#get end time
	endInt = int(time.mktime(datetime.now().timetuple()))
	if endInt-startInt > AUTODUMPINTERVAL:
		autoDump = Global.get("autoDump")
		if not autoDump: autoDump = ""
		recipients = autoDump.split()
		if len(recipients)>0: dumptext = dump(startInt,endInt,True)
		for recipient in recipients:
			sendMail(to=recipient, body=dumptext, subject='SavvyChat Post Dump')

def hlog(text):
	#hacky log
	doPost(str(text),"hlog")
	
def getUserFromId(id):
	return db.GqlQuery("SELECT * FROM Chatuser WHERE userid='"+id+"'").get()
	
def getUserFromName(name):
	return db.GqlQuery("SELECT * FROM Chatuser WHERE name='"+name+"'").get()

def auth(function):
	def newfunction(self, **kwargs):
		#first, authenticate:
		user = users.get_current_user()
		if not user:
			return self.response.out.write("autherror")
		userid = user.user_id()
		kwargs["userid"] = userid
		chatuser = getUserFromId(userid)
		if not chatuser:
			return self.response.out.write("autherror")
		return function(self,**kwargs)
	return newfunction

def userOnline(userid):
	return not not db.GqlQuery("SELECT * FROM Chatchannel WHERE owner='"+userid+"'").get()
	
def makePostObject(post,dontSave=False):
	if dontSave: id="dontSave"
	else:id=post.key().id()
	#convert instance of post class to object ready for JSON
	author = getUserFromId(post.author)
	if not author:
		author = post.author
	else:
		author = author.name
	return {"content":post.content,"author":author,"date":str(time.mktime(post.date.timetuple())),"recipients":post.recipients,"id":id}

def doPost(content, author, dontSave=False, recipients=""):		
	post = Post()
	post.author = author
	post.content = content
	if recipients == "": post.recipients = []
	else: post.recipients = recipients.split(",")
	if not dontSave:
		post.pendingemails = [True]*len(post.recipients)
		call(post)
		post.put()
	broadcast(post,dontSave)
	if not dontSave:
		return post.key().id() #id for the post

def declareUpdate(self):
	lastDate = datetime.now().date()
	dateArg = self.request.get('d')
	if dateArg:
		lastDate = datetime.fromtimestamp(int(dateArg)).date()

	Global.set("lastUpdate",str(lastDate))
	return self.response.out.write("done")

def dump(startInt, endInt, saveLastDump):
	if endInt - startInt > 1000000 or endInt <  startInt:
		#request will probably time out, we need to cut the interval
		endInt = startInt + 1000000

	startDate = datetime.fromtimestamp(startInt)
	endDate = datetime.fromtimestamp(endInt)

	outString = "This is a dump of all posts from " + str(startDate) + " to " + str(endDate) + ".\n(UNIX timestamps " + str(startInt) + "-" + str(endInt) + ")."

	#fetch posts
	postsData = db.GqlQuery("SELECT * FROM Post WHERE date > DATETIME('" + str(startDate) + "') ORDER BY date ASC")
	for post in postsData:
		if post.date > endDate:
			break
		dateString = str(datetime.fromtimestamp(int(time.mktime(post.date.timetuple()))))
		try:
			authorString = getUserFromId(post.author).name
		except AttributeError:
			authorString = post.author
		outString += "\n\n" + authorString + " @ " + dateString + ":\n" + post.content
	if(saveLastDump):
		Global.set("lastDump", str(endInt))
	return outString

def manualDump(self):
	#make a dump of all posts
	#get start time
	try:
		startInt = int(Global.get("lastDump"))
	except TypeError:
		firstPost=db.GqlQuery("SELECT * FROM Post ORDER BY date ASC").get()
		if firstPost is None:
			return
		else:
			startInt = int(time.mktime(firstPost.date.timetuple()))
	startArg = self.request.get('s')
	if startArg:
		startInt = int(startArg)
	
	#get end time
	endInt = int(time.mktime(datetime.now().timetuple()))
	endArg = self.request.get('e')
	if endArg:
		endInt = int(endArg)

	saveLastDump = False
	if self.request.get('l'):
		saveLastDump = True

	self.response.headers['Content-Type'] = "text/plain"
	return self.response.out.write(dump(startInt, endInt, saveLastDump))

def makeWhitelist():
	whiteusers = []
	whiteData = db.GqlQuery("SELECT * FROM Whiteuser")
	for whiteuser in whiteData:
		whiteusers += [whiteuser.email+" "+whiteuser.name]
	return '\n'.join(whiteusers)

def modifyWhitelist(self):
	#first delete all
	whiteData = db.GqlQuery("SELECT * FROM Whiteuser")
	for whiteuser in whiteData:
		db.delete(whiteuser)

	whitelist = self.request.get("whitelist").split('\n')
	for emailData in whitelist:
		try:
			email, name = tuple(emailData.rstrip().split(" "))
		except ValueError:
			continue
		whiteuser = Whiteuser()
		whiteuser.name = name
		whiteuser.email = email
		whiteuser.put()
	return self.response.out.write(makeWhitelist())

def makeAliaslist(onlyChatusers=False,ignoreUser=None):
	aliases = []
	if onlyChatusers:
		chatuserList = []
		chatuserQuery = db.GqlQuery("SELECT * FROM Chatuser")
		for chatuser in chatuserQuery:
			if chatuser.name != ignoreUser:
				chatuserList += [chatuser.name.lower()]
				aliases += [chatuser.name.lower()+" "+chatuser.name.lower()]
	aliasData = db.GqlQuery("SELECT * FROM Alias")
	for alias in aliasData:
		if onlyChatusers:
			aliasusers = alias.meaning.lower().split(",")
			meaning = ",".join(list(set(chatuserList).intersection(aliasusers)))
		else:
			meaning = alias.meaning
		if meaning != "":
			aliases += [alias.aliastext+" "+meaning]
	if onlyChatusers:
		aliases += ["all "+",".join(chatuserList)]
	return '\n'.join(aliases)

def modifyAliases(self):
#first delete all`
	aliasData = db.GqlQuery("SELECT * FROM Alias")
	for alias in aliasData:
		db.delete(alias)

	aliaslist = self.request.get("aliaslist").split('\n')
	for item in aliaslist:
		try:
			aliastext, meaning = tuple(item.rstrip().split(" "))
		except ValueError:
			continue
		alias = db.GqlQuery("SELECT * FROM Alias WHERE aliastext='" + aliastext + "'").get()
		if not alias:
			alias = Alias()
		alias.aliastext = aliastext
		alias.meaning = meaning
		alias.put()
	return self.response.out.write(makeAliaslist())

def initNetloc(self):
	return Global.set("netloc", urlparse.urlsplit(self.request.url)[1])

def getNetloc(self=None):
	netloc = Global.get("netloc")
	if not netloc:
		if self: initNetloc(self)
	return netloc

class PostPage(webapp.RequestHandler):
	@auth
	def post(self,**kwargs):
		#called on recieving a post
		recipients = self.request.get('r')
		if not recipients: recipients = ""
		self.response.out.write(str(doPost(
			self.request.get('p'),
			kwargs["userid"],
			recipients=recipients)))

class callAckPage(webapp.RequestHandler):
	@auth
	def post(self,**kwargs):
		post = Post.get_by_id(int(self.request.get('id')))
		post.pendingemails[post.recipients.index(getUserFromId(user.user_id()).name.lower())] = False
		adjustPendingEmails(post)
		post.put()
		
class RetrievePage(webapp.RequestHandler):
	#there is a request for more of the post archive
	@auth
	def post(self,**kwargs):
		cursor = self.request.get('c')
		numPosts = EXTRAPOSTS
		if self.request.get('n'):
			numPosts = min(int(self.request.get('n')),MAXPOSTS)
		postsData = db.GqlQuery("SELECT * FROM Post ORDER BY date DESC").with_cursor(cursor)
		posts = []
		showarchive = False
		querycursor = ""
		for post in postsData:
			if len(posts) == numPosts:
				#we are done
				showarchive = True
				break
			posts = posts + [makePostObject(post)]
			querycursor = postsData.cursor() #but it's inefficient to keep overwriting the cursor...
		message = simplejson.dumps({'posts':posts,'cursor':querycursor,'showarchive':showarchive})
		self.response.out.write(message)

class SyncPage(webapp.RequestHandler):
	@auth
	def post(self,**kwargs):
		#there is a request for a sync since last time
		endcursor = self.request.get('c')
		if endcursor == "":
			endcursor = None
		postsData = db.GqlQuery("SELECT * FROM Post ORDER BY date DESC").with_cursor(end_cursor=endcursor) #not sure if i can omit start_cursor
		posts = []
		showarchive = False
		startcursor = ""
		for post in postsData:
			if startcursor == "":
				startcursor = postsData.cursor()
			posts = posts + [makePostObject(post)]
		if endcursor:
			#endcursor should have been slightly earlier, chop off the last element
			#this is inefficient
			posts = posts[:-1]
		message = simplejson.dumps({'posts':posts,'cursor':startcursor})
		self.response.out.write(message)

class PingPage(webapp.RequestHandler):
	@auth
	def post(self,**kwargs):
		tokenid = self.request.get('t')
		if tokenid:
			channel.send_message(tokenid, simplejson.dumps({"ping":"ping"}))
			return self.response.out.write("ping")

class HeartbeatPage(webapp.RequestHandler):
	def post(self,**kwargs):
		tokenid = self.request.get('t')
		if tokenid:
			channel.send_message(tokenid, simplejson.dumps({"heartbeat":"heartbeat"}))
			self.response.out.write("heartbeat")
		
class TokenPage(webapp.RequestHandler):
	@auth
	def post(self,**kwargs):
		#this creates a new token, for when an old one expires
		chatchannel = getFreeChannel(kwargs["userid"])
		#serve token
		self.response.out.write(chatchannel.key().name() + '@@' + chatchannel.token)
		
class disconnectPage(webapp.RequestHandler):
	def post(self,**kwargs):
		tokenid = self.request.get('from')

		chatchannel = Chatchannel.get_by_key_name(tokenid)
		if chatchannel and chatchannel.id:
			chatuser = getUserFromId(chatchannel.owner)
			if chatuser:
				chatuser.lastonline = datetime.now()
				chatuser.put()
				chatchannel.owner = None
				chatchannel.put()

		#gotta do this stuff somewhere
		housekeeping()
		
class OptionsPage(webapp.RequestHandler):
	@auth
	def post(self,**kwargs):
		#authenticate
		user = users.get_current_user()
		if not user:
			return
		chatuser = getUserFromId(user.user_id())
		if not chatuser:
			return
		typeString = self.request.get('type')
		if not typeString:
			return
		elif typeString == "tone":
			chatuser.playtone = self.request.get('a') == "true"
		elif typeString == "hf":
			chatuser.hf = self.request.get('h') == "true"
		elif typeString == "shiftsend":
			chatuser.shiftsend = self.request.get('s') == "true"

class MainPage(webapp.RequestHandler):
	def get(self,**kwargs):
		#authenticate
		user = users.get_current_user()
		if not user:
			#not logged in, redirect to login page
			return self.redirect(users.create_login_url(self.request.uri))
		userid = user.user_id()
		logouturl = users.create_logout_url(self.request.uri)
		
		#check if userid is in DB
		chatuser = getUserFromId(userid)
		if chatuser:
			#user already exists
			pass
		else:
			#not in DB
			#check against email whitelist
			whiteuser = db.GqlQuery("SELECT * FROM Whiteuser WHERE email='" + user.email().lower() + "'").get()
			if whiteuser:
				#user is in whitelist
				chatuser = Chatuser()
				chatuser.userid = userid
				chatuser.name = whiteuser.name
				chatuser.email = whiteuser.email
				chatuser.put()
				chatuser.lastonline = datetime(2000,1,1) #so we load every message for them
			else:
				#not in whitelist
				return self.response.out.write(template.render('deny.htm', {'logouturl':logouturl}))
		
		#get unread posts
		postsData = db.GqlQuery("SELECT * FROM Post ORDER BY date DESC")
		posts = []
		#breakTime = False
		#countdown = -1
		showarchive = False
		endquerycursor = ""
		startquerycursor = ""
		#the cursor saves the position in the query.
		for post in postsData:
			if startquerycursor == "":
				#i wish i could initialize this outside the loop
				startquerycursor = postsData.cursor()
			showarchive = True
			if len(posts) == MAXPOSTS:
				#stop sending posts
				break
			if post.date < chatuser.lastonline:
				#this post is too old
				if len(posts) == 0:
					#we should at least show one post
					pass
				else:
					break
			posts = posts + [makePostObject(post)]
			endquerycursor = postsData.cursor() #but it's inefficient to keep overwriting the cursor...
			showarchive = False
		
		chatchannel = getFreeChannel(userid)
		
		#get subtitle
		subtitleData = open("subtitles.txt")
		subtitle = choice(subtitleData.readlines())
		subtitleData.close()
		
		#determine options
		playTone = chatuser.playtone
		shiftSend = chatuser.shiftsend
		hf = chatuser.hf
		
		#get topic
		topic = Global.get('topic')
		if not topic: topic = "Welcome to SavvyChat!"

		lastUpdate = Global.get("lastUpdate")
		if not lastUpdate:
			lastUpdate = ""
		else:
			lastUpdate = " Last updated on " + lastUpdate + "."

		disableMath = False
		if self.request.get('disableMath'):
			disableMath = True
		
		suppressErrors = False
		if self.request.get('suppressErrors'):
			suppressErrors = True

		theme = self.request.get('theme')
		if not theme:
			theme = "white"
		
		gadget = False
		v = ""
		container = ""
		libs = ""
		if self.request.get('gadget'):
			gadget = True
			v = self.request.get('v')
			container = self.request.get('container')
			libs = self.request.get('libs')
			theme = "gadget"
		
		#inject template values and render
		template_values = {'token': chatchannel.token,
							'aliaslist': makeAliaslist(True,chatuser.name),
							'posts': posts,
							'userid': userid,
							'tokenid': chatchannel.key().name(),
							'name':chatuser.name,
							'logouturl':logouturl,
							'subtitle':subtitle,
							'topic':topic,
							'lastUpdate':lastUpdate,
							'showarchive':showarchive,
							'startquerycursor':startquerycursor,
							'endquerycursor':endquerycursor,
							'playTone':playTone,
							'shiftSend':shiftSend,
							'hf':hf,
							'gadget':gadget,
							'v':v,
							'libs':libs,
							'container':container,
							'disableMath':disableMath,
							'suppressErrors':suppressErrors,
							'theme':theme}
		self.response.out.write(template.render('index.htm', template_values))

class AdminPage(webapp.RequestHandler):
	#special admin functions
	def get(self,**kwargs):
		#authenticate
		user = users.get_current_user()
		if not user:
			#not logged in, redirect to login page
			return self.redirect(users.create_login_url('/'))
		admins = Global.get("admins")
		if not admins:
			admins = "test@example.com"
			Global.set("admins", admins)
		if not user.email().lower() in admins:
			return self.redirect('/')

		typeString = self.request.get('type')
		if not typeString:
			autoDump = Global.get("autoDump")
			if not autoDump: autoDump = ""
			return self.response.out.write(template.render('admin.htm', {
				'lastDump':getLastDump(),
				'autoDump':autoDump,
				'whitelist':makeWhitelist(),
				'aliases':makeAliaslist()
			}))
		if typeString == "dump":
			return manualDump(self)
		if typeString == "autodump":
			recipients = self.request.get('r')
			if recipients:
				Global.set("autoDump",recipients)
				return
			return
		if typeString == "init":
			return initNetloc(self)
		if typeString == "white":
			return modifyWhitelist(self)
		if typeString == "aliases":
			return modifyAliases(self)
		if typeString == "date":
			return declareUpdate(self)

class UploadPage(webapp.RequestHandler):
	@auth
	def post(self,**kwargs):
		fileobj = self.request.POST["file"]
		#internet explorer gives full path, reduce it:
		filename = urllib.quote_plus(re.sub(r"^.*\\","",fileobj.filename))
		file = File()
		file.data = fileobj.value
		file.type = fileobj.type
		file.name = filename
		try:
			file.put()
		except:
			doPost("File upload failed","SavvyChat",dontSave=True)
			return
		path = "download/"+str(file.key().id()) + "/" + filename
		fullpath = "http://" + self.request.host + "/" + path
		post = "uploaded file: [[" + fullpath+ "]]"
		if file.type.find("image") != -1:
			post = "uploaded image:\n[[img@@" + fullpath+ "]]"
		doPost(post,kwargs["userid"])
		self.response.out.write(path)
		#self.response.headers['Content-Type'] = file.type
		#self.response.headers['Content-Disposition'] = "attachment; filename=" + file.name
		#self.response.out.write(file.data)
		
class DownloadHandler(webapp.RequestHandler):
	def get(self, id, filename,**kwargs):
		file = File.get_by_id(int(id))
		self.response.headers['Content-Type'] = file.type
		self.response.out.write(file.data)

class GadgetXMLPage(webapp.RequestHandler):
	def get(self,**kwargs):
		disableMath = False
		if self.request.get('disableMath'):
			disableMath = True
		template_values = {'netloc':getNetloc(self),'disableMath':disableMath}
		return self.response.out.write(template.render('gadget.xml', template_values))

class HelpPage(webapp.RequestHandler):
	@auth
	def get(self,**kwargs):
		return self.response.out.write(template.render('help.htm', {
			'netloc':getNetloc(self),
			'aliaslist': makeAliaslist(True)
		}))
		
application = webapp.WSGIApplication([
									('/', MainPage),
									('/post', PostPage),
									('/callack', callAckPage),
									('/retrieve', RetrievePage),
									('/sync', SyncPage),
									('/ping', PingPage),
									('/heartbeat', HeartbeatPage),
									('/_ah/channel/disconnected/', disconnectPage),
									('/options', OptionsPage),
									('/token', TokenPage),
									('/upload', UploadPage),
									('/admin', AdminPage),
									('/download/(\d+)/(.*)', DownloadHandler),
									('/gadget\\.xml', GadgetXMLPage),
									('/help', HelpPage)])

def main():
	#housekeeping()
	run_wsgi_app(application)

if __name__ == "__main__":
	main()
