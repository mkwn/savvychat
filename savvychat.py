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

import re

import traceback

EXTRAPOSTS = 10 #number of posts recieved before last unread. CANNOT BE ZERO

#http://stackoverflow.com/questions/2350454/simplest-way-to-store-a-value-in-google-app-engine-python
class Global(db.Model):
	#store miscellaneous strings
	value = db.StringProperty()

	@classmethod
	def get(cls, key):
		instance = cls.get_by_key_name(key)
		if not instance:
			return "None"
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
	lastonline = db.DateTimeProperty(auto_now=True)

def fetchTokens():
	#tokens = memcache.get("tokens")
	#if not tokens:
	
	tokens=[]	
	chatusers = db.GqlQuery("SELECT * FROM Chatuser WHERE tokens!=NULL")
	for chatuser in chatusers:
		tokens = tokens + chatuser.tokens
	return tokens

def broadcast(post):
	#send a message to everyone who is online
	tokens = fetchTokens()
	message = simplejson.dumps(makePostObject(post))
	for token in tokens:
		channel.send_message(token, message)
		
def call(recipients=[], author="a user", content=""):
	if recipients == []:
		return
	#send out emails
	TOarray=[]
	if recipients == ["all"]:
		#call all
		chatusers = db.GqlQuery("SELECT * FROM Chatuser")
		for chatuser in chatusers:
			TOarray = TOarray + [chatuser.name + ' <' + chatuser.email+'>']
		pass
	else:
		for recipient in recipients:
			chatuser = getUserFromName(recipient.title())
			#this isnt working, maybe uppercaseness
			if chatuser:
				TOarray = TOarray + [chatuser.name + ' <' + chatuser.email+'>']
	if TOarray == []:
		return
	mail.send_mail(sender='SavvyChat Mailer <mailer@savvychat.appspotmail.com>',
					to=', '.join(TOarray),
					subject="You have been called by "+author+" with SavvyChat",
					body="Here is an unformatted preview of the post:\n\n"+content)

def removeWhitespace(data):
	return ''.join(data.split())
		
def resolveStragglers():
	#if a computer crashes, they will not be able to send the onClose message
	#look for users with no updates in 2 hours and close them
	chatusers = db.GqlQuery("SELECT * FROM Chatuser")
	now=datetime.now()
	for chatuser in chatusers:
		expired=False
		for idx, lastrefresh in enumerate(chatuser.lastrefreshlist):
			if (now-lastrefresh).seconds > 60*60*2+120:
				#expired
				del chatuser.lastrefreshlist[idx]
				del chatuser.tokens[idx]
				expired=True
		if expired:
			chatuser.put()
		
def hlog(text):
	#hacky log
	post = Post()
	post.author = "hlog"
	post.content = str(text)
	post.put()
	
def getUserFromId(id):
	return db.GqlQuery("SELECT * FROM Chatuser WHERE userid='"+id+"'").get()
	
def getUserFromName(name):
	return db.GqlQuery("SELECT * FROM Chatuser WHERE name='"+name+"'").get()
	
def makePostObject(post):
	#convert instance of post class to object ready for JSON
	author=getUserFromId(post.author)
	if not author:
		author = post.author
	else:
		author = author.name
	return {"content":post.content,"author":author,"date":str(time.mktime(post.date.timetuple())),"recipients":post.recipients}

class OpenedPage(webapp.RequestHandler):
	def post(self):
		#called when a new connection is created
		chatuser = getUserFromId(self.request.get('u'))
		#chatuser = Chatuser()
		chatuser.tokens = chatuser.tokens + [self.request.get('t')]
		chatuser.lastrefreshlist = chatuser.lastrefreshlist + [datetime.utcnow()]
		chatuser.put()

	
class PostPage(webapp.RequestHandler):
	def post(self):
		#called on recieving a post
		post = Post()
		post.author = self.request.get('u')
		post.content = self.request.get('p')
		
		post.recipients = []#for call
		calltext = post.content
		
		#check for meta
		m = re.match(r"\|+([^\|]+?)(\|+|$)([\s\S]*)", post.content)
		if m:
			#we have meta
			meta,calltext = m.group(1,3)
			meta = removeWhitespace(meta.lower())
			
			#check if topic was changed
			if meta == "topic":
				Global.set('topic',post.content.replace("\n"," "))
				
			#check if users were called
			colonIndex = meta.find(":")
			if meta=="call":
				#no arguments, call all
				post.recipients=["all"]
			elif meta[:colonIndex] == "call":
				callstring = ','+meta[colonIndex+1:].lower()+','
				#replace aliases
				aliasData = open("aliases.txt")
				aliasList = aliasData.readlines()
				aliasData.close()
				for aliasRow in aliasList:
					#replace aliases
					alias, result = tuple(aliasRow.rstrip().split(" "))
					callstring = callstring.replace(","+alias+",",","+result+",")
				post.recipients = list(Set(callstring.split(",")[1:-1]))
				if "all" in post.recipients:
					post.recipients=["all"]

		post.put()
		broadcast(post)
		authoruser = getUserFromId(post.author)
		authoruser.put()
		call(post.recipients,authoruser.name,calltext)
			
class RetrievePage(webapp.RequestHandler):
	def post(self):
		#there is a request for more of the post archive
		cursor = self.request.get('c')
		postsData = db.GqlQuery("SELECT * FROM Post ORDER BY date DESC").with_cursor(cursor)
		countdown = EXTRAPOSTS
		posts = []
		breakTime = False
		showarchive = False
		querycursor = ""
		for post in postsData:
			if breakTime:
				showarchive = True
				break
			if countdown == 1:
				querycursor = postsData.cursor() #i don't actually understand this
			if countdown == 0:
				breakTime = True
				continue
			posts = posts+[makePostObject(post)]
			if countdown > 0:
				countdown = countdown - 1
				continue
		
		#postList = postsData.fetch(EXTRAPOSTS)
		message = simplejson.dumps({'posts':posts,'cursor':querycursor,'showarchive':showarchive})
		channel.send_message(self.request.get('t'), message)
		
class TokenPage(webapp.RequestHandler):
	def post(self):
		#called when connection expires
		#remove old token in db
		userid = self.request.get('u')
		chatuser = getUserFromId(userid)
		tokenindex = chatuser.tokens.index(self.request.get('t'))
		del chatuser.tokens[tokenindex]
		del chatuser.lastrefreshlist[tokenindex]
		chatuser.put()
		#get new token
		suffix = 0
		while 1:
			if not userid+str(suffix) in chatuser.tokens:
				tokenid = userid+str(suffix)
				break
			suffix = suffix + 1
		token = channel.create_channel(tokenid)
		self.response.out.write(tokenid + '@@' + token)
		
class ClosedPage(webapp.RequestHandler):
	def post(self):
		#called when someone disconnects
		#remove token
		chatuser = getUserFromId(self.request.get('u'))
		tokenindex = chatuser.tokens.index(self.request.get('t'))
		del chatuser.tokens[tokenindex]
		del chatuser.lastrefreshlist[tokenindex]
		chatuser.put()
		
class TonePage(webapp.RequestHandler):
	def post(self):
		#called when someone changes the alert tone settings
		#remove token
		chatuser = getUserFromId(self.request.get('u'))
		tone = self.request.get('a')
		if tone == "true":
			chatuser.playtone = True
		else:
			chatuser.playtone = False
		chatuser.put()
		
class MainPage(webapp.RequestHandler):
	def get(self):
		#authenticate, create a token and render the main page
		
		user = users.get_current_user()
		if not user:
			#not logged in, redirect to login page
			path = '/'
			if self.request.get('gadget'):
				path = '/?gadget=true'
			return self.redirect(users.create_login_url(path))
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
			whitelistData = open("whitelist.txt")
			whitelist = whitelistData.readlines()
			whitelistData.close()
			for emailData in whitelist+[""]:
				if emailData.rstrip()=="":
					#not in whitelist
					self.response.out.write(template.render('deny.htm', {'logouturl':logouturl}))
					return
				email, name = tuple(emailData.rstrip().split(" "))
				if email == user.email().lower():
					#user is in whitelist
					chatuser = Chatuser()
					chatuser.userid=userid
					chatuser.name=name
					chatuser.email=email
					chatuser.put()
					break
		
		#get unread posts, plus EXTRAPOSTS more
		postsData = db.GqlQuery("SELECT * FROM Post ORDER BY date DESC")
		posts = []
		breakTime = False
		countdown = -1
		showarchive = False
		querycursor = ""
		for post in postsData:
			if breakTime:
				showarchive = True
				break
			if countdown == 1:
				querycursor = postsData.cursor() #i don't actually understand this
			if countdown == 0:
				breakTime = True
				continue
			posts = posts+[makePostObject(post)]
			if countdown > 0:
				countdown = countdown - 1
				continue
			if post.date < chatuser.lastonline:
				countdown = EXTRAPOSTS - 1
				
		#make token
		suffix = 0
		while 1:
			if not userid+str(suffix) in chatuser.tokens:
				tokenid = userid+str(suffix)
				break
			suffix = suffix + 1
		token = channel.create_channel(tokenid)
		
		#get subtitle
		subtitleData = open("subtitles.txt")
		subtitle = choice(subtitleData.readlines())
		subtitleData.close()
		
		#determine tone alert default
		disableAlert = not chatuser.playtone
		
		#get topic
		topic = Global.get('topic')
		
		disableMath=False
		if self.request.get('disableMath'):
			disableMath=True
		
		gadget = False
		v = ""
		container = ""
		libs = ""
		if self.request.get('gadget'):
			gadget = True
			v = self.request.get('v')
			container = self.request.get('container')
			libs = self.request.get('libs')
		
		#inject template values and render
		template_values = {'token': token,
							'posts': posts,
							'userid': userid,
							'tokenid': tokenid,
							'name':chatuser.name,
							'logouturl':logouturl,
							'subtitle':subtitle,
							'topic':topic,
							'showarchive':showarchive,
							'querycursor':querycursor,
							'disableAlert':disableAlert,
							'gadget':gadget,
							'v':v,
							'libs':libs,
							'container':container,
							'disableMath':disableMath}
		self.response.out.write(template.render('index.htm', template_values))
		
application = webapp.WSGIApplication([
									('/', MainPage),
									('/opened', OpenedPage),
									('/post', PostPage),
									('/retrieve', RetrievePage),
									('/tone', TonePage),
									('/token', TokenPage),
									('/closed', ClosedPage)])

def main():
	resolveStragglers()
	run_wsgi_app(application)

if __name__ == "__main__":
	main()