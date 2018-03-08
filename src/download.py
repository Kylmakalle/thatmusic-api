import os
import stat
from typing import Dict

from tornado import web
import aiohttp

from cache import CachedHandler
from settings import PATHS, HASH, DOWNLOAD_SETTINGS


# TODO add S3 support
# noinspection PyAbstractClass
class DownloadHandler(CachedHandler):
    def __init__(self, application, request, **kwargs):
        super().__init__(application, request, **kwargs)
        self._cache_path = PATHS['mp3']

    @web.addslash
    async def get(self, *args, **kwargs):
        await self.download(kwargs['key'], kwargs['id'], stream=False)

    def write_error(self, status_code, **kwargs):
        self.finish({'success': 0, 'error': self._reason, 'error_code': status_code})

    async def download(self, cache_key: str, audio_id: str, stream: bool = False):  # TODO add bitrate convertor
        file_path = self._build_file_path(audio_id)

        if os.path.exists(file_path):
            audio_item = self._get_audio_item_cache(audio_id)
            if audio_item is None:
                audio_item = self._get_audio_item_from_cached_search(cache_key, audio_id)
            if audio_item is None:
                audio_name = '{}.mp3'.format(audio_id)
            else:
                audio_name = self._format_audio_name(audio_item)

            await self._download_from_local_cache(file_path, audio_name, stream)
            raise web.Finish()

        audio_item = self._get_audio_item_from_cached_search(cache_key, audio_id)
        if audio_item is None:
            raise web.HTTPError(404)
        audio_name = self._format_audio_name(audio_item)

        if not await self._download_file(audio_item['mp3'], file_path):
            raise web.HTTPError(404)

        await self._download_from_local_cache(file_path, audio_name, stream)

    # TODO add proxy support
    @staticmethod
    async def _download_file(url: str, path: str):
        try:
            with open(path, 'wb') as f:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=DOWNLOAD_SETTINGS['timeout']) as response:
                        async for chunk in response.content.iter_chunked(64 * 1024):
                            f.write(chunk)
        except aiohttp.ClientError:
            if os.path.exists(path):
                os.remove(path)
            return False

        return True

    async def _download_from_local_cache(self, path: str, file_name: str, stream: bool):
        if self._check_is_bad_mp3(path):
            raise web.HTTPError(404)

        self._set_headers(path, file_name)
        # TODO check
        if stream:
            for chunk in self._get_content(path):
                self.write(chunk)
                await self.flush()
        else:
            with open(path, 'rb') as f:
                self.write(f.read())
            await self.flush()

    def _set_headers(self, path: str, file_name: str):
        self.set_header('Cache-Control', 'private')
        self.set_header('Cache-Description', 'File Transfer')
        self.set_header('Content-Type', 'audio/mpeg')
        self.set_header('Content-Length', self._get_content_size(path))
        self.set_header('Content-Disposition', 'attachment; filename={}'.format(file_name))

    @staticmethod
    def _get_content_size(path: str):
        stat_result = os.stat(path)
        return stat_result[stat.ST_SIZE]

    @staticmethod
    def _get_content(path: str):
        with open(path, 'rb') as f:
            chunk_size = 64 * 1024
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    def _get_audio_item_from_cached_search(self, cache_key: str, audio_id: str):
        cached_search_result = self._get_cached_search_result(cache_key)
        if cached_search_result is None:
            return None

        audio_item = None
        for item in cached_search_result:
            if item['id'] == audio_id:
                audio_item = item
                break
        if audio_item is None:
            return None

        self._cache_audio_item(audio_item)

        # TODO mp3 url decoder support
        return audio_item

    @staticmethod
    def _build_file_path(audio_id: str):
        file_name = '{}.mp3'.format(uni_hash(HASH['mp3'], audio_id))
        file_path = os.path.join(PATHS['mp3'], file_name)
        return file_path

    @staticmethod
    def _format_audio_name(audio_item: Dict):
        name = '{} - {}'.format(audio_item['artist'], audio_item['title'])
        # TODO sanitize
        return '{}.mp3'.format(name)

    @staticmethod
    def _check_is_bad_mp3(path: str):
        if not os.path.exists(path):
            return True

        # TODO checks
        # valid_mimes = ['audio/mpeg', 'audio/mp3', 'application/octet-stream']

        return False


# noinspection PyAbstractClass
class StreamHandler(DownloadHandler):
    @web.addslash
    async def get(self, *args, **kwargs):
        await self.download(kwargs['key'], kwargs['id'], stream=True)