import os
import stat
from typing import Dict

from tornado import web
import aiohttp

from cache import CachedHandler
from settings import PATHS, HASH, DOWNLOAD_SETTINGS, SEARCH_SETTINGS

from utils import uni_hash, sanitize, set_id3_tag, vk_url
import uuid
from urllib.parse import unquote


# TODO add S3 support
# noinspection PyAbstractClass
class DownloadHandler(CachedHandler):
    def __init__(self, application, request, **kwargs):
        super().__init__(application, request, **kwargs)
        self._cache_path = PATHS['mp3']
        os.makedirs(self._cache_path, exist_ok=True)

    @web.addslash
    async def get(self, *args, **kwargs):
        await self.download(kwargs['key'], kwargs['id'], stream=False)

    async def download(self, cache_key: str, audio_id: str, stream: bool = False):
        file_path = self._build_file_path(audio_id)

        if os.path.exists(file_path):
            self.logger.debug('Audio file already exist: {}'.format(file_path))
            audio_info = self._get_audio_info_cache(audio_id)
            if audio_info is None:
                audio_info = self._get_audio_info_from_cached_search(cache_key, audio_id)
            if audio_info is None:
                audio_name = '{}.mp3'.format(audio_id)
            else:
                audio_name = self._format_audio_name(audio_info)
        else:
            audio_info = self._get_audio_info_from_cached_search(cache_key, audio_id)
            if audio_info is None:
                raise web.HTTPError(404)
            audio_name = self._format_audio_name(audio_info)

            if not await self._download_audio(audio_info, file_path):
                raise web.HTTPError(502)

        if not await self._send_from_local_cache(file_path, audio_name, stream):
            raise web.HTTPError(502)

    # TODO add proxy support
    async def _download_audio(self, audio_info: Dict, path: str):
        self.logger.debug('Downloading from vk: {}'.format(audio_info['mp3']))
        try:
            with open(path, 'wb') as f:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                            audio_info['mp3'],
                            timeout=DOWNLOAD_SETTINGS['timeout']
                    ) as response:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            f.write(chunk)
            set_id3_tag(path, audio_info)
        except (aiohttp.ClientError, IOError):
            if os.path.exists(path):
                os.remove(path)
            return False

        return True

    async def _send_from_local_cache(self, path: str, file_name: str, stream: bool):
        self.logger.debug('Sending file from local storage [streaming={}]: {}'.format(stream, file_name))
        self._set_headers(path, file_name, stream)
        for chunk in self._get_content(path):
            self.write(chunk)
            if stream:
                await self.flush()
        if not stream:
            await self.flush()
        self.finish()
        return True

    def _set_headers(self, path: str, file_name: str, stream: bool):
        self.set_header('Cache-Control', 'private')
        self.set_header('Cache-Description', 'File Transfer')
        self.set_header('Content-Type', 'audio/mpeg')
        if not stream:
            self.set_header('Content-Length', self._get_content_size(path))
        self.set_header('Content-Disposition', 'attachment; filename={}'.format(file_name))

    def _get_content_size(self, path: str):
        stat_result = os.stat(path)
        size = stat_result[stat.ST_SIZE]
        self.logger.debug('File size: {:.02f}MB'.format(size / 1024 ** 2))
        return size

    @staticmethod
    def _get_content(path: str, chunk_size=64 * 1024):
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(chunk_size), b''):
                yield chunk

    def _get_audio_info_from_cached_search(self, cache_key: str, audio_id: str):
        self.logger.debug('Getting audio item from search cache')
        cached_search_result = self._get_cached_search_result(cache_key)
        if cached_search_result is None:
            return None

        audio_info = None
        for item in cached_search_result:
            if item['id'] == audio_id:
                audio_info = item
                break
        if audio_info is None:
            return None

        self._cache_audio_info(audio_info)
        return audio_info

    @staticmethod
    def _build_file_path(audio_id: str):
        file_name = '{}.mp3'.format(uni_hash(HASH['mp3'], audio_id))
        file_path = os.path.join(PATHS['mp3'], file_name)
        return file_path

    @staticmethod
    def _format_audio_name(audio_info: Dict):
        name = '{} - {}'.format(audio_info['artist'], audio_info['title'])
        name = sanitize(name, to_lower=False, alpha_numeric_only=False)
        return '{}.mp3'.format(name)


# noinspection PyAbstractClass
class StreamHandler(DownloadHandler):
    @web.addslash
    async def get(self, *args, **kwargs):
        await self.download(kwargs['key'], kwargs['id'], stream=True)


class DownloadByIdHandler(DownloadHandler):
    def __init__(self, application, request, **kwargs):
        super().__init__(application, request, **kwargs)
        self._cache_path = PATHS['mp3']
        os.makedirs(self._cache_path, exist_ok=True)

    @web.addslash
    async def get(self, *args, **kwargs):
        await self.download(kwargs['owner'], kwargs['id'], stream=False)

    async def download(self, cache_key: str, audio_id: str, access_key: str = '',
                       token: str = SEARCH_SETTINGS['access_token'],
                       stream: bool = False):
        file_path = self._build_file_path(audio_id)
        if os.path.exists(file_path):
            self.logger.debug('Audio file already exist: {}'.format(file_path))
            audio_info = self._get_audio_info_cache(audio_id)
            if audio_info is None:
                audio_info = self._get_audio_info_from_cached_search(cache_key, audio_id)
            if audio_info is None:
                audio_name = '{}.mp3'.format(audio_id)
            else:
                audio_name = self._format_audio_name(audio_info)
        else:
            audio_info = await self._get_audio_info_by_id(cache_key, audio_id, token, access_key=access_key)

            if audio_info is None:
                raise web.HTTPError(404)
            audio_name = self._format_audio_name(audio_info)

            if not await self._download_audio(audio_info, file_path):
                raise web.HTTPError(502)

        if not await self._send_from_local_cache(file_path, audio_name, stream):
            raise web.HTTPError(502)

    async def _get_audio_info_by_id(self, owner, audio_id, token, access_key=''):
        headers = {'User-Agent': SEARCH_SETTINGS['user_agent']}
        params = {
            'access_token': token,
            'audios': '{}_{}'.format(owner, audio_id),
            'v': '5.78'
        }
        if access_key:
            params['audios'] += '_{}'.format(access_key)

        self.logger.debug('Requesting audio ({}) from vk...'.format(params['audios']))
        log_result = None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        url=vk_url('method/audio.getById'),
                        headers=headers,
                        params=params
                ) as response:
                    result = await response.json()
            log_result = result
            result = result['response'][0]
            if int(result['owner_id']) == 100 or int(result['id']) == 1:
                raise web.HTTPError(502)
            result['mp3'] = result['url']
            result['id'] = str(result['id'])
        except (KeyError, IndexError):
            self.logger.warning('Malformed vk response for audio ({}) ...'.format(params['audios']))
            if log_result:
                self.logger.warning('{}'.format(log_result))
            raise web.HTTPError(502)
        self._cache_audio_info(result)
        return result


class DownloadByIdAccessHandler(DownloadByIdHandler):
    def __init__(self, application, request, **kwargs):
        super().__init__(application, request, **kwargs)
        self._cache_path = PATHS['mp3']
        os.makedirs(self._cache_path, exist_ok=True)

    @web.addslash
    async def get(self, *args, **kwargs):
        await self.download(kwargs['owner'], kwargs['id'], kwargs.get('access_key', ''), token=kwargs['token'],
                            stream=False)


class DownloadByUrlHandler(DownloadHandler):
    def __init__(self, application, request, **kwargs):
        super().__init__(application, request, **kwargs)
        self._cache_path = PATHS['mp3']
        os.makedirs(self._cache_path, exist_ok=True)

    @web.addslash
    async def get(self, *args, **kwargs):
        url = self.get_argument('url')
        if not url:
            raise web.HTTPError(404)
        title = self.get_argument('title', '')
        artist = self.get_argument('artist', '')
        audio_info = {'mp3': url, 'title': title, 'artist': artist}
        await self.download(audio_info, stream=False)

    async def download(self, audio_info: dict, stream: bool):
        file_path = self._build_file_path(str(uuid.uuid4().int))
        if audio_info is None:
            raise web.HTTPError(404)
        audio_name = self._format_audio_name(audio_info)

        if not await self._download_audio(audio_info, file_path):
            raise web.HTTPError(502)

        if not await self._send_from_local_cache(file_path, audio_name, stream):
            raise web.HTTPError(502)
