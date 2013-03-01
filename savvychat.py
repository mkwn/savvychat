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

from random import choice

from sets import Set

import urllib
import urlparse

import re

import traceback

MAXPOSTS = 30 #max number of posts initialized
#EXTRAPOSTS = 30 #number of posts received when "view older" is checked.

#http://stackoverflow.com/questions/2350454/simplest-way-to-store-a-value-in-google-app-engine-python
class Global(db.Model):
	#store miscellaneous strings
	value = db.StringProperty()

	@classmethod
	def get(cls, key):
		instance = cls.get_by_key_name(key)
		if not instance:
			return None
		return cls.get_by_key_name(key).value

	@classmethod
	def set(cls, key, value):
		entity = cls(key_name=key, value=value)
		entity.put()
		return value

class Post(db.Model):
	#each post stored in the DB is of this class
	author = db.StringProperty()
	content = db.TextProperty()#if i use string, there's a 500 character limit
	recipients = db.StringListProperty()#for call
	date = db.DateTimeProperty(auto_now_add=True)

class Chatuser(db.Model):
	#this user data is automatically created on first logon
	userid = db.StringProperty()
	name = db.StringProperty()
	email = db.EmailProperty()
	tokens = db.StringListProperty()
	lastrefreshlist = db.ListProperty(datetime)
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

def fetchTokens():
	#tokens = memcache.get("tokens")
	#if not tokens:
	
	tokens = []	
	chatusers = db.GqlQuery("SELECT * FROM Chatuser WHERE tokens!=NULL")
	for chatuser in chatusers:
		tokens = tokens + chatuser.tokens
	return tokens

def delToken(chatuser, idx):
	try:
		del chatuser.lastrefreshlist[idx]
	except IndexError: pass
	try:
		del chatuser.tokens[idx]
	except IndexError: pass

def broadcast(post):
	#send a message to everyone who is online
	tokens = fetchTokens()
	message = simplejson.dumps(makePostObject(post))
	for token in tokens:
		channel.send_message(token, message)

def decompressHTML(html):
	workhtml = html
	pairs=[("b>","</b>"),
		("<b","<b>"),		
		("i>","</i>"),
		("<i","<i>"),
		("<s",'<span style="text-decoration:line-through;">'),
		("s>","</span>"),
		("<q",'<blockquote style="border:solid;border-left:none;border-right:none;border-width: 1px;">'),
		("q>","</blockquote>"),
		("<t","<div>Highlight below to show "),
		("t>",":</div>"),
		("<p",'<blockquote style="background-color:white;color:white;">'),
		("p>","</blockquote>"),
		("<l","<li>"),
		("l>","</li>"),
		("<u","<ul>"),
		("u>","</ul>"),
		("<c",'<span style="font-family:Courier New,monospace;background-color:#EEE;">'),
		("c>","</span>"),
		("<x",'<pre style="font-family:Courier New,monospace;background-color:#EEE;">'),
		("x>","</pre>"),
		("<a",'<a href="'),
		("<>",'">'),
		("a>","</a>"),
		("<m",'<span style="font-family:Courier New,monospace;">'),
		("m>","</span>"),
		("<z",'<div style="font-family:Courier New,monospace;text-align:center;">'),
		("z>","</div>"),
		("<r","<br>")]
	for p in pairs:
		workhtml = workhtml.replace(p[0],p[1])
	return workhtml

def sendMail(to, subject, body, html=None, senderName='SavvyChat Mailer', senderMail='mailer'):
	netloc = getNetloc()
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
		
def call(self,recipients=[], author="a user", plaincontent="", htmlcontent=""):
	if recipients == []:
		return
	#send out emails
	TOarray = []
	if recipients == ["all"]:
		#call all
		chatusers = db.GqlQuery("SELECT * FROM Chatuser")
		for chatuser in chatusers:
			TOarray = TOarray + [chatuser.name + ' <' + chatuser.email + '>']
		pass
	else:
		for recipient in recipients:
			chatuser = getUserFromName(recipient.title())
			if chatuser:
				TOarray = TOarray + [chatuser.name + ' <' + chatuser.email + '>']
	if TOarray == []:
		return
	sendMail(to=', '.join(TOarray),
		subject="You have been called by "+author+" with SavvyChat",
		body=plaincontent,
		html=htmlcontent)
	netloc = getNetloc().split('.')

def removeWhitespace(data):
	return ''.join(data.split())
		
def resolveStragglers():
	setMC = False
	lastFlush = memcache.get("lastFlush")
	if not lastFlush:
		setMC = True
		lastFlush = Global.get("lastFlush")
		if not lastFlush:
			lastFlush = "1"
	lastFlushInt = int(lastFlush)
	nowInt = int(time.mktime(datetime.utcnow().timetuple()))
	if setMC:
		memcache.add(key="lastFlush", value=lastFlush)
	
	if nowInt-lastFlushInt < 60*60:
		return
	#if a computer crashes, they will not be able to send the onClose message
	#look for users with no updates in 2 hours and close them
	chatusers = db.GqlQuery("SELECT * FROM Chatuser")
	now = datetime.now()

	for chatuser in chatusers:
		expired = False
		for idx, lastrefresh in enumerate(chatuser.lastrefreshlist):
			if (now-lastrefresh).seconds > 60*60*2+120:
				#expired
				delToken(chatuser, idx)
				expired = True
		if expired:
			chatuser.put()

	memcache.set(key="lastFlush", value=str(nowInt))
	Global.set("lastFlush", str(nowInt))
		
def checkDump():
	#check if its been about a day since the last dump
	#get start time
	try:
		startInt = int(Global.get("lastDump"))
	except TypeError:
		firstPost=db.GqlQuery("SELECT * FROM Post ORDER BY date ASC").get()
		if firstPost is None:
			return
		else:
			startInt = int(time.mktime(firstPost.date.timetuple()))

	#get end time
	endInt = int(time.mktime(datetime.utcnow().timetuple()))
	if endInt-startInt > 24*60*60:
		sendMail(to="mattk210@gmail.com", body=dump(startInt,endInt), subject='SavvyChat Post Dump')

def hlog(text):
	#hacky log
	doPost(str(text),"hlog")
	
def getUserFromId(id):
	return db.GqlQuery("SELECT * FROM Chatuser WHERE userid='"+id+"'").get()
	
def getUserFromName(name):
	return db.GqlQuery("SELECT * FROM Chatuser WHERE name='"+name+"'").get()
	
def makePostObject(post):
	#convert instance of post class to object ready for JSON
	author = getUserFromId(post.author)
	if not author:
		author = post.author
	else:
		author = author.name
	return {"content":post.content,"author":author,"date":str(time.mktime(post.date.timetuple())),"recipients":post.recipients,"id":post.key().id()}

def doPost(content, author=None, dontSave=False):
	if not author:
		user = users.get_current_user()
		if not user:
			return
		authoruser = getUserFromId(user.user_id())
		if not authoruser:
			return
		
	post = Post()
	post.author = author or user.user_id()
	post.content = content

	post.recipients = []

	if dontSave:
		post.date = datetime.now()
	else:
		post.put()
	broadcast(post)
	if not author:
		authoruser.lastonline = datetime.utcnow()
		authoruser.put()
	try:
		return post.key().id() #id for the post
	except:
		pass
	#call(post.recipients,authoruser.name,contenttext)

def declareUpdate(self):
	lastDate = datetime.utcnow().date()
	dateArg = self.request.get('d')
	if dateArg:
		lastDate = datetime.fromtimestamp(int(dateArg)).date()

	Global.set("lastUpdate",str(lastDate))
	return self.response.out.write("done")

def dump(startInt, endInt):
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
	endInt = int(time.mktime(datetime.utcnow().timetuple()))
	endArg = self.request.get('e')
	if endArg:
		endInt = int(endArg)

	self.response.headers['Content-Type'] = "text/plain"
	return self.response.out.write(dump(startInt, endInt))

def initWhite(self):
	whitelistData = open("whitelist.txt")
	whitelist = whitelistData.readlines()
	whitelistData.close()
	for emailData in whitelist:
		if emailData.rstrip() == "":
			#end of whitelist
			break
		email, name = tuple(emailData.rstrip().split(" "))
		whiteuser = db.GqlQuery("SELECT * FROM Whiteuser WHERE email='" + email + "'").get()
		if whiteuser: continue
		whiteuser = Whiteuser()
		whiteuser.name = name
		whiteuser.email = email
		whiteuser.put()
	return self.response.out.write("done")

def initNetloc(self):
	return Global.set("netloc", urlparse.urlsplit(self.request.url)[1])

def getNetloc():
	netloc = Global.get("netloc")
	if not netloc:
		Global.set("netloc","savvychat")
		netloc = "savvychat.appspot.com"
	return netloc

class PostPage(webapp.RequestHandler):
	def post(self):
		#called on recieving a post
		self.response.out.write(str(doPost(self.request.get('p'))))
		
class CallPage(webapp.RequestHandler):
	def post(self):
		user = users.get_current_user()
		if not user:
			return
		chatuser = getUserFromId(user.user_id())
		if not chatuser:
			return
		recipients=[]
		callstring = ',' + self.request.get('r').lower() + ','
		if callstring == ",,":
			#no arguments, call all
			recipients = ["all"]
		else:
			#replace aliases
			aliasData = open("aliases.txt")
			aliasList = aliasData.readlines()
			aliasData.close()
			for aliasRow in aliasList:
				#replace aliases
				alias, result = tuple(aliasRow.rstrip().split(" "))
				callstring = callstring.replace(","+alias+",",","+result+",")
			recipients = list(Set(callstring.split(",")[1:-1]))
			if "all" in recipients:
				recipients = ["all"]
		chatuser.lastonline = datetime.utcnow()
		chatuser.put()
		call(self,recipients,chatuser.name,
			"no plaintext preview available, sign in to savvychat (http://savvychat.appspot.com) to view message",
			decompressHTML(self.request.get('h')))
			
class RetrievePage(webapp.RequestHandler):
	#there is a request for more of the post archive
	def post(self):
		cursor = self.request.get('c')
		numPosts = 20
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
				break;
			posts = posts + [makePostObject(post)]
			querycursor = postsData.cursor() #but it's inefficient to keep overwriting the cursor...
		#postList = postsData.fetch(EXTRAPOSTS)
		message = simplejson.dumps({'posts':posts,'cursor':querycursor,'showarchive':showarchive})
		self.response.out.write(message)
		#channel.send_message(self.request.get('t'), message)

class SyncPage(webapp.RequestHandler):
	#there is a request for a sync since last time
	def post(self):
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
		#channel.send_message(self.request.get('t'), message)

class PingPage(webapp.RequestHandler):
	def post(self):
		token = self.request.get('t')
		if token:
			channel.send_message(token, simplejson.dumps({"ping":"ping"}))
			return self.response.out.write("ping") #for abort logic
		
class TokenPage(webapp.RequestHandler):
	#this creates a new token, for when an old one expires
	def post(self):
		#first, authenticate:
		user = users.get_current_user()
		if not user:
			self.response.out.write("autherror")
			return
		userid = user.user_id()
		chatuser = getUserFromId(userid)
		if not chatuser:
			self.response.out.write("autherror")
			return

		#next, remove old token in db
		try:
			tokenindex = chatuser.tokens.index(self.request.get('t'))
			delToken(chatuser, tokenindex)
		except ValueError:
			#token has been removed already
			pass

		#get new token
		suffix = 0
		while 1:
			if not userid+str(suffix) in chatuser.tokens:
				tokenid = userid+str(suffix)
				break
			suffix = suffix + 1
		token = channel.create_channel(tokenid)
		
		#save changes to db
		chatuser.tokens = chatuser.tokens + [tokenid]
		chatuser.lastrefreshlist = chatuser.lastrefreshlist + [datetime.utcnow()]
		chatuser.lastonline = datetime.utcnow()
		chatuser.put()
		
		#serve token
		self.response.out.write(tokenid + '@@' + token)
		
class ClosedPage(webapp.RequestHandler):
	#This is for when someone disconnects, to get rid of their token
	def post(self):
		chatuser = getUserFromId(self.request.get('u'))
		tokenindex = chatuser.tokens.index(self.request.get('t'))
		try:
			tokenindex = chatuser.tokens.index(self.request.get('t'))
			delToken(chatuser, tokenindex)
		except ValueError:
			#token has been removed already
			pass
		if not self.request.get('e'):
			#we didn't error out, this user has seen all messages
			chatuser.lastonline = datetime.utcnow()
		chatuser.put()

		#gotta do this stuff somewhere
		resolveStragglers()
		checkDump()
		
class OptionsPage(webapp.RequestHandler):
	#for when someone changes some setting
	def post(self):
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

		chatuser.lastonline = datetime.utcnow()
		chatuser.put()

class MainPage(webapp.RequestHandler):
	def get(self):
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
				#chatuser.put()
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
				
		#make token
		suffix = 0
		while 1:
			if not userid+str(suffix) in chatuser.tokens:
				tokenid = userid+str(suffix)
				break
			suffix = suffix + 1
		token = channel.create_channel(tokenid)
		
		#the following would ideally be in openedpage, but it creates the possibility of duplicate tokens
		chatuser.tokens = chatuser.tokens + [tokenid]
		chatuser.lastrefreshlist = chatuser.lastrefreshlist + [datetime.utcnow()]
		chatuser.lastonline = datetime.utcnow()
		chatuser.put()
		
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
		template_values = {'token': token,
							'posts': posts,
							'userid': userid,
							'tokenid': tokenid,
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
	def get(self):
		#authenticate
		user = users.get_current_user()
		if not user:
			#not logged in, redirect to login page
			return self.redirect(users.create_login_url('/'))
		if user.email().lower() != "mattk210@gmail.com":
			return self.redirect('/')

		typeString = self.request.get('type')
		if not typeString:
			return self.response.out.write('<a href="?type=dump">Dump posts</a><br /><a href="?type=white">Initialize whitelist</a><br /><a href="?type=date">Declare update</a><br /><a href="?type=netloc">Initialize netloc</a>')
		if typeString == "dump":
			return manualDump(self)
		if typeString == "white":
			return initWhite(self)
		if typeString == "date":
			return declareUpdate(self)
		if typeString == "netloc":
			return initNetloc(self)

class UploadPage(webapp.RequestHandler):
	def post(self):
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
			doPost("File upload failed","SavvyChat",dontSend=True)
			return
		path = "download/"+str(file.key().id()) + "/" + filename
		fullpath = "http://" + self.request.host + "/" + path
		post = "[[" + fullpath+ "]]"
		if file.type.find("image") != -1:
			post = "|img|" + fullpath
		doPost(post)
		self.response.out.write(path)
		#self.response.headers['Content-Type'] = file.type
		#self.response.headers['Content-Disposition'] = "attachment; filename=" + file.name
		#self.response.out.write(file.data)
		
class DownloadHandler(webapp.RequestHandler):
	def get(self, id, filename):
		file = File.get_by_id(int(id))
		self.response.headers['Content-Type'] = file.type
		self.response.out.write(file.data)

class GadgetXMLPage(webapp.RequestHandler):
	def get(self):
		disableMath = False
		if self.request.get('disableMath'):
			disableMath = True
		template_values = {'netloc':getNetloc(),'disableMath':disableMath}
		return self.response.out.write(template.render('gadget.xml', template_values))

class HelpPage(webapp.RequestHandler):
	def get(self):
		return self.response.out.write(template.render('help.htm', {'netloc':getNetloc()}))
		
application = webapp.WSGIApplication([
									('/', MainPage),
									('/post', PostPage),
									('/call', CallPage),
									('/retrieve', RetrievePage),
									('/sync', SyncPage),
									('/ping', PingPage),
									('/options', OptionsPage),
									('/token', TokenPage),
									('/upload', UploadPage),
									('/admin', AdminPage),
									('/download/(\d+)/(.*)', DownloadHandler),
									('/gadget\\.xml', GadgetXMLPage),
									('/help', HelpPage),
									('/closed', ClosedPage)])

def main():
	#resolveStragglers()
	#checkDump()
	run_wsgi_app(application)

if __name__ == "__main__":
	main()
