async def cmd_play(self, message, player, channel, author, permissions, leftover_args, song_url):
      """
      Usage:
          {command_prefix}play song_link
          {command_prefix}play text to search for
          {command_prefix}play spotify_uri

      Adds the song to the playlist.  If a link is not provided, the first
      result from a youtube search is added to the queue.

      If enabled in the config, the bot will also support Spotify URIs, however
      it will use the metadata (e.g song name and artist) to find a YouTube
      equivalent of the song. Streaming from Spotify is not possible.
      """

      song_url = song_url.strip('<>')

      await self.send_typing(channel)

      if leftover_args:
          song_url = ' '.join([song_url, *leftover_args])
      leftover_args = None  # prevent some crazy shit happening down the line

      # Make sure forward slashes work properly in search queries
      linksRegex = '((http(s)*:[/][/]|www.)([a-z]|[A-Z]|[0-9]|[/.]|[~])*)'
      pattern = re.compile(linksRegex)
      matchUrl = pattern.match(song_url)
      song_url = song_url.replace('/', '%2F') if matchUrl is None else song_url

      # Rewrite YouTube playlist URLs if the wrong URL type is given
      playlistRegex = r'watch\?v=.+&(list=[^&]+)'
      matches = re.search(playlistRegex, song_url)
      groups = matches.groups() if matches is not None else []
      song_url = "https://www.youtube.com/playlist?" + groups[0] if len(groups) > 0 else song_url

      if self.config._spotify:
          if 'open.spotify.com' in song_url:
              song_url = 'spotify:' + re.sub('(http[s]?:\/\/)?(open.spotify.com)\/', '', song_url).replace('/', ':')
          if song_url.startswith('spotify:'):
              parts = song_url.split(":")
              try:
                  if 'track' in parts:
                      res = await self.spotify.get_track(parts[-1])
                      song_url = res['artists'][0]['name'] + ' ' + res['name']

                  elif 'album' in parts:
                      res = await self.spotify.get_album(parts[-1])
                      await self._do_playlist_checks(permissions, player, author, res['tracks']['items'])
                      procmesg = await self.safe_send_message(channel, self.str.get('cmd-play-spotify-album-process', 'Processing album `{0}` (`{1}`)').format(res['name'], song_url))
                      for i in res['tracks']['items']:
                          song_url = i['name'] + ' ' + i['artists'][0]['name']
                          log.debug('Processing {0}'.format(song_url))
                          await self.cmd_play(message, player, channel, author, permissions, leftover_args, song_url)
                      await self.safe_delete_message(procmesg)
                      return Response(self.str.get('cmd-play-spotify-album-queued', "Enqueued `{0}` with **{1}** songs.").format(res['name'], len(res['tracks']['items'])))

                  elif 'playlist' in parts:
                      res = []
                      r = await self.spotify.get_playlist_tracks(parts[-1])
                      while True:
                          res.extend(r['items'])
                          if r['next'] is not None:
                              r = await self.spotify.make_spotify_req(r['next'])
                              continue
                          else:
                              break
                      await self._do_playlist_checks(permissions, player, author, res)
                      procmesg = await self.safe_send_message(channel, self.str.get('cmd-play-spotify-playlist-process', 'Processing playlist `{0}` (`{1}`)').format(parts[-1], song_url))
                      for i in res:
                          song_url = i['track']['name'] + ' ' + i['track']['artists'][0]['name']
                          log.debug('Processing {0}'.format(song_url))
                          await self.cmd_play(message, player, channel, author, permissions, leftover_args, song_url)
                      await self.safe_delete_message(procmesg)
                      return Response(self.str.get('cmd-play-spotify-playlist-queued', "Enqueued `{0}` with **{1}** songs.").format(parts[-1], len(res)))

                  else:
                      raise exceptions.CommandError(self.str.get('cmd-play-spotify-unsupported', 'That is not a supported Spotify URI.'), expire_in=30)
              except exceptions.SpotifyError:
                  raise exceptions.CommandError(self.str.get('cmd-play-spotify-invalid', 'You either provided an invalid URI, or there was a problem.'))

      async with self.aiolocks[_func_() + ':' + str(author.id)]:
          if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
              raise exceptions.PermissionsError(
                  self.str.get('cmd-play-limit', "You have reached your enqueued song limit ({0})").format(permissions.max_songs), expire_in=30
              )

          if player.karaoke_mode and not permissions.bypass_karaoke_mode:
              raise exceptions.PermissionsError(
                  self.str.get('karaoke-enabled', "Karaoke mode is enabled, please try again when its disabled!"), expire_in=30
              )

          try:
              info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
          except Exception as e:
              if 'unknown url type' in str(e):
                  song_url = song_url.replace(':', '')  # it's probably not actually an extractor
                  info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
              else:
                  raise exceptions.CommandError(e, expire_in=30)

          if not info:
              raise exceptions.CommandError(
                  self.str.get('cmd-play-noinfo', "That video cannot be played. Try using the {0}stream command.").format(self.config.command_prefix),
                  expire_in=30
              )

          log.debug(info)

          if info.get('extractor', '') not in permissions.extractors and permissions.extractors:
              raise exceptions.PermissionsError(
                  self.str.get('cmd-play-badextractor', "You do not have permission to play media from this service."), expire_in=30
              )

          # abstract the search handling away from the user
          # our ytdl options allow us to use search strings as input urls
          if info.get('url', '').startswith('ytsearch'):
              # print("[Command:play] Searching for \"%s\"" % song_url)
              info = await self.downloader.extract_info(
                  player.playlist.loop,
                  song_url,
                  download=False,
                  process=True,    # ASYNC LAMBDAS WHEN
                  on_error=lambda e: asyncio.ensure_future(
                      self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
                  retry_on_error=True
              )

              if not info:
                  raise exceptions.CommandError(
                      self.str.get('cmd-play-nodata', "Error extracting info from search string, youtubedl returned no data. "
                                                      "You may need to restart the bot if this continues to happen."), expire_in=30
                  )

              if not all(info.get('entries', [])):
                  # empty list, no data
                  log.debug("Got empty list, no data")
                  return

              # TODO: handle 'webpage_url' being 'ytsearch:...' or extractor type
              song_url = info['entries'][0]['webpage_url']
              info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
              # Now I could just do: return await self.cmd_play(player, channel, author, song_url)
              # But this is probably fine

          # TODO: Possibly add another check here to see about things like the bandcamp issue
          # TODO: Where ytdl gets the generic extractor version with no processing, but finds two different urls

          if 'entries' in info:
              await self._do_playlist_checks(permissions, player, author, info['entries'])

              num_songs = sum(1 for _ in info['entries'])

              if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
                  try:
                      return await self._cmd_play_playlist_async(player, channel, author, permissions, song_url, info['extractor'])
                  except exceptions.CommandError:
                      raise
                  except Exception as e:
                      log.error("Error queuing playlist", exc_info=True)
                      raise exceptions.CommandError(self.str.get('cmd-play-playlist-error', "Error queuing playlist:\n`{0}`").format(e), expire_in=30)

              t0 = time.time()

              # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
              # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
              # I don't think we can hook into it anyways, so this will have to do.
              # It would probably be a thread to check a few playlists and get the speed from that
              # Different playlists might download at different speeds though
              wait_per_song = 1.2

              procmesg = await self.safe_send_message(
                  channel,
                  self.str.get('cmd-play-playlist-gathering-1', 'Gathering playlist information for {0} songs{1}').format(
                      num_songs,
                      self.str.get('cmd-play-playlist-gathering-2', ', ETA: {0} seconds').format(fixg(
                          num_songs * wait_per_song)) if num_songs >= 10 else '.'))

              # We don't have a pretty way of doing this yet.  We need either a loop
              # that sends these every 10 seconds or a nice context manager.
              await self.send_typing(channel)

              # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
              #       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

              entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

              tnow = time.time()
              ttime = tnow - t0
              listlen = len(entry_list)
              drop_count = 0

              if permissions.max_song_length:
                  for e in entry_list.copy():
                      if e.duration > permissions.max_song_length:
                          player.playlist.entries.remove(e)
                          entry_list.remove(e)
                          drop_count += 1
                          # Im pretty sure there's no situation where this would ever break
                          # Unless the first entry starts being played, which would make this a race condition
                  if drop_count:
                      print("Dropped %s songs" % drop_count)

              log.info("Processed {} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
                  listlen,
                  fixg(ttime),
                  ttime / listlen if listlen else 0,
                  ttime / listlen - wait_per_song if listlen - wait_per_song else 0,
                  fixg(wait_per_song * num_songs))
              )

              await self.safe_delete_message(procmesg)

              if not listlen - drop_count:
                  raise exceptions.CommandError(
                      self.str.get('cmd-play-playlist-maxduration', "No songs were added, all songs were over max duration (%ss)") % permissions.max_song_length,
                      expire_in=30
                  )

              reply_text = self.str.get('cmd-play-playlist-reply', "Enqueued **%s** songs to be played. Position in queue: %s")
              btext = str(listlen - drop_count)

          else:
              if info.get('extractor', '').startswith('youtube:playlist'):
                  try:
                      info = await self.downloader.extract_info(player.playlist.loop, 'https://www.youtube.com/watch?v=%s' % info.get('url', ''), download=False, process=False)
                  except Exception as e:
                      raise exceptions.CommandError(e, expire_in=30)

              if permissions.max_song_length and info.get('duration', 0) > permissions.max_song_length:
                  raise exceptions.PermissionsError(
                      self.str.get('cmd-play-song-limit', "Song duration exceeds limit ({0} > {1})").format(info['duration'], permissions.max_song_length),
                      expire_in=30
                  )

              try:
                  entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

              except exceptions.WrongEntryTypeError as e:
                  if e.use_url == song_url:
                      log.warning("Determined incorrect entry type, but suggested url is the same.  Help.")

                  log.debug("Assumed url \"%s\" was a single entry, was actually a playlist" % song_url)
                  log.debug("Using \"%s\" instead" % e.use_url)

                  return await self.cmd_play(player, channel, author, permissions, leftover_args, e.use_url)

              reply_text = self.str.get('cmd-play-song-reply', "Enqueued `%s` to be played. Position in queue: %s")
              btext = entry.title


          if position == 1 and player.is_stopped:
              position = self.str.get('cmd-play-next', 'Up next!')
              reply_text %= (btext, position)

          else:
              try:
                  time_until = await player.playlist.estimate_time_until(position, player)
                  reply_text += self.str.get('cmd-play-eta', ' - estimated time until playing: %s')
              except:
                  traceback.print_exc()
                  time_until = ''

              reply_text %= (btext, position, ftimedelta(time_until))

      return Response(reply_text, delete_after=30)
