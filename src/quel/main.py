# coding: utf8

from pony import orm
from quel import db
from quel.db.utils import create_or_update
from quel.core.client import Client, EventMultiplexer, EventType, event, get_event, set_event, propagate_event
from quel.core.handlers import on, command
from quel.core.reloader import Reloader
from quel.providers import ResolveError
from quel.providers.rawfile import RawFileProvider
from quel.providers.soundcloud import SoundCloudProvider
from quel.providers.youtube_dl import YoutubeDlProvider
from urllib.parse import urlparse

import argparse
import asyncio
import discord
import logging
import json
import os
import random
import re
import sys


providers = [
  SoundCloudProvider(),
  RawFileProvider(),
  YoutubeDlProvider(allow_video_stream=False)
]

logger = logging.getLogger(__name__)

reloader = Reloader()


@orm.db_session
def get_guild(guild_id=None):
  guild_id = guild_id or event.message.guild.id
  return create_or_update(db.Guild, {'id': guild_id})


class GuildSongResumer:

  def __init__(self, quel):
    self.queue = asyncio.Queue()
    self.quel = quel
    self.task = None

  def start(self):
    self.task = asyncio.create_task(self.run())

  async def run(self):
    while True:
      guild, ev = await self.queue.get()
      try:
        with set_event(ev):
          await self.quel.resume(force=True)
      except:
        logger.exception('Exception in GuildSongResumer.run()')

  async def put(self, guild):
    await self.queue.put((guild, get_event()))



class QuelBehavior(EventMultiplexer):

  nickname = '♪♪ Quel ♪♪'

  # Thanks to https://textfac.es/
  welcome_smileys = [
    '(▀̿Ĺ̯▀̿ ̿)',
    '(づ｡◕‿‿◕｡)づ',
    '~(˘▾˘~)',
    'ヾ(⌐■_■)ノ♪',
    'ლ(´ڡ`ლ)',
    'ƪ(˘⌣˘)ʃ',
    '〆(・∀・＠)',
  ]

  def __init__(self, config):
    super().__init__()
    self.config = config
    self.song_resumer = GuildSongResumer(self)

  def check_mention(self):
    match = re.match('^\s*<@!?(\d+)>\s*', event.text)
    if match and match.group(1) == str(self.client.user.id):
      event.text = event.text[match.end():]
      return True
    return False

  def check_channel(self, channel):
    if isinstance(channel, discord.TextChannel) and channel.topic:
      return 'quel' in channel.topic.lower()
    return False

  async def handle_event(self):
    if event.type == EventType.message:
      if event.message.author == self.client.user:
        return False
      if not (self.check_mention() or self.check_channel(event.message.channel)):
        return False
    return await super().handle_event()

  async def update_nick(self, guild):
    if not guild.me.nick:
      try:
        await guild.me.edit(nick=self.nickname)
      except discord.Forbidden:
        pass

  @on('ready')
  async def ready(self):
    client_id = (await self.client.application_info()).id
    invite_url = self.config['botConfig']['inviteUrl'].format(CLIENT_ID=client_id)
    logger.info('Invite URL: {}'.format(invite_url))
    logger.info('Loading providers for all servers.')
    for guild in self.client.guilds:
      await self.update_nick(guild)
      await self.provider_reload(guild)

      # Say hello in Quel's main channel.
      for channel in guild.channels:
        if self.check_channel(channel):
          await channel.send("I'm b{}ck! {}".format('a' * random.randint(1, 15), random.choice(self.welcome_smileys)))
          break

    self.song_resumer.start()

  @on('guild_join')
  async def guild_join(self):
    await self.update_nick(event.guild)

  @on('message')
  async def handle_plain_attachment(self):
    if event.text or not event.message.attachments:
      return False

    song_urls = []
    for attachment in event.message.attachments:
      url = attachment.url
      if RawFileProvider().match_url(url, urlparse(url)):
        song_urls.append(url)

    if song_urls:
      await self.play('play', ';'.join(song_urls))
    return True

  @command(regex='config\s+set\s+([\w\d\.]+)\s+(.*)')
  async def config_set(self, key, value):
    with orm.db_session:
      guild = get_guild()
      guild.config[key] = value
      await self.provider_reload()

  @command(regex='config\s+del\s+([\w\d.]+)')
  async def config_del(self, key):
    with orm.db_session:
      guild = get_guild()
      guild.config.pop(key, None)
      await self.provider_reload()

  @command(regex='providers?\s+reload')
  async def provider_reload(self, guild=None):
    with orm.db_session:
      guild = get_guild(guild.id if guild else None)
      guild.init_providers(logger, providers, force=True)

  @command(regex='providers?\s+status')
  async def provider_status(self):
    guild = get_guild()
    if not guild.providers:
      await event.reply('No providers installed.')
    for provider in guild.providers:
      message = provider.error or 'Ok'
      await event.reply('**{}**: {}'.format(provider.provider.name, message))

  @command(regex='providers?\s+help')
  async def provider_help(self):
    for provider in providers:
      lines = ['- ' + x for x in provider.get_option_names()]
      await event.reply('**{}**\n```\n{}\n```'.format(provider.name, '\n'.join(lines)))

  @command(regex='search\s+(?:(\w+):\s*)?(.*)')
  async def search(self, provider_name, term):
    with orm.db_session:
      guild = get_guild()
    if provider_name:
      provider_name = provider_name.lower()
      for provider in providers:
        if provider.id.lower() == provider_name or provider.name.lower() == provider_name:
          break
      else:
        await event.reply('"{}" is an unknown provider'.format(provider_name))
        return
      for instance in guild.providers:
        if not instance.error and instance.provider == provider:
          break
      else:
        await event.reply('Provider "{}" is not (properly) installed'.format(provider.name))
        return
      search_providers = [instance]
    else:
      search_providers = [x for x in guild.providers if not x.error and x.supports_search()]
      if not search_providers:
        await event.reply('No providers available.')
        return

    provider_names = ['**{}**'.format(x.name) for x in search_providers]
    await event.reply('Searching "{}" in {}'.format(term, ', '.join(provider_names)))

    embed = discord.Embed(title='Results for "{}"'.format(term))
    for provider in search_providers:
      async for song in provider.search(term, 5):
        embed.add_field(name=song.title, value=song.url)
    await event.reply(embed=embed)

  @command(regex='(queue|play)\s+(.*)', flags=re.S)
  async def play(self, command, arg):
    guild = get_guild()
    errors = []
    songs = []
    for url in map(str.strip, arg.split(';')):
      if not url: continue
      if url.startswith('<') and url.endswith('>'):
        url = url[1:-1]
      urlinfo = urlparse(url)
      if not urlinfo.netloc or not urlinfo.scheme:
        errors.append('Invalid URL `{}`'.format(url))
      else:
        song = None
        for provider in guild.providers:
          matches, match_data = provider.match_url(url, urlinfo)
          if not provider.error and matches:
            try:
              song = await provider.resolve_url(url, match_data)
            except ResolveError as exc:
              errors.append('`{}`: {}'.format(url, exc))
              break
            song = db.QueuedSong(
              user_id=event.message.author.id,
              provider_id=provider.id,
              **song.asdict())
            break
        else:
          errors.append('No provider for URL `{}`'.format(url))
          continue
        if not song:
          continue
        async with guild.lock:
          guild.queue_song(song)
          await event.reply('Queued **{}** - {} (by {})'.format(song.title, song.artist, event.message.author.mention))
        if command == 'play':
          await self.resume()
          command = None   # Don't call resume() for the next songs

    if errors:
      await event.reply('\n'.join(errors))

  @command(regex='resume')
  async def resume(self, force=False):
    guild = get_guild()
    async with guild.lock:
      if not force and guild.voice_client and guild.voice_client.source:
        guild.voice_client.resume()
        return

      if not guild.queue:
        if guild.voice_client:
          await guild.voice_client.disconnect()
          guild.voice_client = None
        return

      song = guild.queue.pop(0)
      provider = guild.find_provider(song.provider_id)
      if not provider:
        logger.error('Provider for queued Song no longer exists: {}'.format(song.provider_id))
        return

      if not guild.voice_client:
        voice_state = event.message.author.voice
        voice_channel = voice_state.channel if voice_state else None
        if not voice_channel:
          await event.reply('Join a voice channel and type `resume` to start playing music!')
          return
        guild.voice_client = await voice_channel.connect()
        # We wait for a second as otherwise we get speed up music right after
        # the bot joined.
        await asyncio.sleep(1)

      stream_url = await provider.get_stream_url(song)

      # Call skip() after the song is complete. We need to maintain the
      # event state.
      do_skip = propagate_event(lambda: self.song_resumer.put(guild))
      loop = asyncio.get_running_loop()
      after = lambda _: asyncio.run_coroutine_threadsafe(do_skip(), loop)

      await guild.start_stream(stream_url, after)

    user = await self.client.get_user_info(song.user_id)
    await event.reply('Now playing! **{}** - {} (queued by {})'.format(song.title, song.artist, user.mention))

  @command(regex='pause')
  async def pause(self):
    guild = get_guild()
    async with guild.lock:
      if guild.voice_client and guild.voice_client.is_playing():
        guild.voice_client.pause()

  @command(regex='(skip)')
  async def skip(self, as_command=None):
    guild = get_guild()
    async with guild.lock:
      if guild.voice_client and guild.voice_client.is_playing():
        guild.voice_client.stop()

  @command(regex='clear\s+queue')
  async def clear_queue(self):
    guild = get_guild()
    async with guild.lock:
      guild.queue = []

  @command(regex='stop(\s*)(!+)?')
  async def stop(self, ws=None, exclam=None):
    ws = ws or ''
    exclam = exclam or ''

    guild = get_guild()
    async with guild.lock:
      if guild.voice_client:
        guild.voice_client.stop()
        await guild.voice_client.disconnect()
        guild.voice_client = None
      else:
        return
    if len(exclam) > 0:
      response = 'Ok'
      if len(exclam) >= 3:
        response += ' dude chill out!'
      else:
        response += ws + exclam*2
      await event.reply(response)

  @command(regex='queue')
  async def queue(self):
    with orm.db_session:
      guild = get_guild()

    lines = ['**Queue**']
    embed = discord.Embed(title='Queued songs')
    async with guild.lock:
      for song in guild.queue:
        user = await self.client.get_user_info(song.user_id)
        embed.add_field(name=song.title, value='{} (queued by {})'.format(song.artist, user.mention), inline=False)
        lines.append('{} - {} (queued by {})'.format(song.title, song.artist, user.mention))
    try:
      await event.reply(embed=embed)
    except discord.Forbidden:
      await event.reply('\n'.join(lines))

  @command(regex='volume(?:\s+(\d+))?')
  async def volume(self, value):
    with orm.db_session:
      guild = get_guild()
      if value is None:
        await event.reply('Current volume is **{}**'.format(int(round(guild.volume * 100))))
      else:
        guild.set_volume(int(value) / 100)

  @command(regex='reload')
  async def reload(self):
    if reloader.is_inner():
      reloader.send_reload()
    else:
      await event.reply('Reloading not enabled.')

  @command(regex='.*')
  async def fallback(self):
    await event.reply('{} ?'.format(event.message.author.mention))


"""
@command(regex='config\s+conversation')
async def config_conversation():
  user = self.local.message.author
  private_channel = await self.client.start_private_message(user)
  async def reply(message):
    return await self.client.send_message(private_channel, message)
  await reply('Hi! For the next 5 minutes you can send commands to me in '
              'private. Use this to set up credentials for music providers.')
  self.user_for_server[user.id] = self.local.message.server
"""


def main():
  parser = argparse.ArgumentParser()
  parser.add_argument('-c', '--config', default='config.json')
  parser.add_argument('-v', '--verbose', action='store_true')
  parser.add_argument('-r', '--reload', action='store_true')
  parser.add_argument('--prod', '--production', dest='production', action='store_true')
  args = parser.parse_args()

  with open(args.config) as fp:
    config = json.load(fp)

  loglevel = logging.INFO if args.verbose else logging.WARNING
  logformat = config.get('logging', {}).get('format')
  if not logformat:
    logformat = '[%(levelname)s %(name)s %(asctime)s]: %(message)s'
  logging.basicConfig(format=logformat, level=loglevel)

  if args.reload and not reloader.is_inner():
    logger.info('Starting reloader ...')
    return reloader.run_forever([sys.executable, '-m', 'quel.main'] + sys.argv[1:])

  logger.info('Binding database ...')
  if 'filename' in config['dbConfig']:
    config['dbConfig']['filename'] = os.path.abspath(config['dbConfig']['filename'])
  db.db.bind(**config['dbConfig'])
  db.db.generate_mapping(create_tables=True)

  bot_config = config['botConfig']
  if 'token' in bot_config:
    token = bot_config['token']
  elif args.production:
    token = bot_config['productionToken']
  else:
    token = bot_config['developmentToken']

  logger.info('Starting ...')

  client = Client()
  client.add_handler(QuelBehavior(config))
  client.run(token)

  logger.info('Bye bye.')


if __name__ == '__main__':
  main()
