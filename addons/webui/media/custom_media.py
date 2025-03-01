import asyncio
import errno
import fractions
import logging
import threading
import time
import av

from typing import Dict, Optional, Set
from aiortc.mediastreams import AUDIO_PTIME, MediaStreamError, MediaStreamTrack

logger = logging.getLogger(__name__)


REAL_TIME_FORMATS = [
    "alsa",
    "android_camera",
    "avfoundation",
    "bktr",
    "decklink",
    "dshow",
    "fbdev",
    "gdigrab",
    "iec61883",
    "jack",
    "kmsgrab",
    "openal",
    "oss",
    "pulse",
    "sndio",
    "rtsp",
    "v4l2",
    "vfwcap",
    "x11grab",
]


async def blackhole_consume(track):
    while True:
        try:
            await track.recv()
        except MediaStreamError:
            return


class MediaBlackhole:
    """
    A media sink that consumes and discards all media.
    """

    def __init__(self):
        self.__tracks = {}

    def addTrack(self, track):
        """
        Add a track whose media should be discarded.

        :param track: A :class:`aiortc.MediaStreamTrack`.
        """
        if track not in self.__tracks:
            self.__tracks[track] = None

    async def start(self):
        """
        Start discarding media.
        """
        for track, task in self.__tracks.items():
            if task is None:
                self.__tracks[track] = asyncio.ensure_future(blackhole_consume(track))

    async def stop(self):
        """
        Stop discarding media.
        """
        for task in self.__tracks.values():
            if task is not None:
                task.cancel()
        self.__tracks = {}


def player_worker(loop, container, streams, audio_track, video_track, quit_event, throttle_playback):
    audio_fifo = av.AudioFifo()
    audio_format_name = "s16"
    audio_layout_name = "stereo"
    audio_sample_rate = 48000
    audio_samples = 0
    audio_samples_per_frame = int(audio_sample_rate * AUDIO_PTIME)
    audio_resampler = av.AudioResampler(
        format=audio_format_name, layout=audio_layout_name, rate=audio_sample_rate
    )

    video_first_pts = None

    frame_time = None
    start_time = time.time()

    while not quit_event.is_set():
        try:
            frame = next(container.decode(*streams))
        except (av.AVError, StopIteration) as exc:
            if isinstance(exc, av.FFmpegError) and exc.errno == errno.EAGAIN: # type: ignore
                time.sleep(0.01)
                continue
            if audio_track:
                asyncio.run_coroutine_threadsafe(audio_track._queue.put(None), loop)
            if video_track:
                asyncio.run_coroutine_threadsafe(video_track._queue.put(None), loop)
            break

        # read up to 1 second ahead
        if throttle_playback:
            elapsed_time = time.time() - start_time
            if frame_time and frame_time > elapsed_time + 1:
                time.sleep(0.1)

        if isinstance(frame, av.AudioFrame) and audio_track:
            if (
                frame.format.name != audio_format_name
                or frame.layout.name != audio_layout_name
                or frame.sample_rate != audio_sample_rate
            ):
                frame.pts = None
                frame = audio_resampler.resample(frame)

            # fix timestamps
            frame.pts = audio_samples
            frame.time_base = fractions.Fraction(1, audio_sample_rate)
            audio_samples += frame.samples

            audio_fifo.write(frame)
            while True:
                frame = audio_fifo.read(audio_samples_per_frame)
                if frame:
                    frame_time = frame.time
                    asyncio.run_coroutine_threadsafe(
                        audio_track._queue.put(frame), loop
                    )
                else:
                    break
        elif isinstance(frame, av.VideoFrame) and video_track:
            if video_track.max_frame_to_decode > 0:
                first = True
                if frame.index > video_track.max_frame_to_decode:  #d_ while
                    if first:
                        logger.info('Decoded %d frames, max to decode %d, awaiting' % \
                                        (frame.index, video_track.max_frame_to_decode))
                        first = False
                    time.sleep(1)                                  #d_ 0.1
                    if quit_event.is_set():
                        return
                if not first:
                    logger.info('Continuing decoding')

            if frame.pts is None:  # pragma: no cover
                logger.warning(
                    "MediaPlayer(%s) Skipping video frame with no pts", container.name
                )
                continue

            # video from a webcam doesn't start at pts 0, cancel out offset
            if video_first_pts is None:
                video_first_pts = frame.pts
            frame.pts -= video_first_pts

            frame_time = frame.time
            asyncio.run_coroutine_threadsafe(video_track._queue.put(frame), loop)


class PlayerStreamTrack(MediaStreamTrack):
    def __init__(self, player, kind):
        super().__init__()
        self.kind = kind
        self._player = player
        self._queue = asyncio.Queue()
        self._start = None
        self.max_frame_to_decode = -1

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError

        self._player._start(self) # type: ignore
        frame = await self._queue.get()
        if frame is None:
            self.stop()
            raise MediaStreamError
        frame_time = frame.time

        # control playback rate
        if (
            self._player is not None
            and self._player._throttle_playback
            and frame_time is not None
        ):
            if self._start is None:
                self._start = time.time() - frame_time
            else:
                wait = self._start + frame_time - time.time()
                await asyncio.sleep(wait)

        return frame

    def stop(self):
        super().stop()
        if self._player is not None:
            self._player._stop(self)
            self._player = None

    # <= 0 means no limit
    def set_max_frame_to_decode(self, frame_num):
        self.max_frame_to_decode = frame_num


class MediaPlayer:
    """
    A media source that reads audio and/or video from a file.

    Examples:

    .. code-block:: python

        # Open a video file.
        player = MediaPlayer('/path/to/some.mp4')

        # Open an HTTP stream.
        player = MediaPlayer(
            'http://download.tsi.telecom-paristech.fr/'
            'gpac/dataset/dash/uhd/mux_sources/hevcds_720p30_2M.mp4')

        # Open webcam on Linux.
        player = MediaPlayer('/dev/video0', format='v4l2', options={
            'video_size': '640x480'
        })

        # Open webcam on OS X.
        player = MediaPlayer('default:none', format='avfoundation', options={
            'video_size': '640x480'
        })

        # Open webcam on Windows.
        player = MediaPlayer('video=Integrated Camera', format='dshow', options={
            'video_size': '640x480'
        })

    :param file: The path to a file, or a file-like object.
    :param format: The format to use, defaults to autodect.
    :param options: Additional options to pass to FFmpeg.
    """

    def __init__(self, file, format=None, options={}):
        self.__container = av.open(file=file, format=format, mode="r", options=options)
        self.__thread: Optional[threading.Thread] = None
        self.__thread_quit: Optional[threading.Event] = None

        # examine streams
        self.__started: Set[PlayerStreamTrack] = set()
        self.__streams = []
        self.__audio: Optional[PlayerStreamTrack] = None
        self.__video: Optional[PlayerStreamTrack] = None
        for stream in self.__container.streams:
            if stream.type == "audio" and not self.__audio:
                self.__audio = PlayerStreamTrack(self, kind="audio")
                self.__streams.append(stream)
            elif stream.type == "video" and not self.__video:
                self.__video = PlayerStreamTrack(self, kind="video")
                self.__streams.append(stream)

        # check whether we need to throttle playback
        container_format = set(self.__container.format.name.split(","))
        self._throttle_playback = not container_format.intersection(REAL_TIME_FORMATS)

    @property
    def audio(self) -> MediaStreamTrack:
        """
        A :class:`aiortc.MediaStreamTrack` instance if the file contains audio.
        """
        return self.__audio # type: ignore

    @property
    def video(self) -> MediaStreamTrack:
        """
        A :class:`aiortc.MediaStreamTrack` instance if the file contains video.
        """
        return self.__video # type: ignore

    def _start(self, track: PlayerStreamTrack) -> None:
        self.__started.add(track)
        if self.__thread is None:
            self.__log_debug("Starting worker thread")
            self.__thread_quit = threading.Event()
            self.__thread = threading.Thread(
                name="media-player",
                target=player_worker,
                args=(
                    asyncio.get_event_loop(),
                    self.__container,
                    self.__streams,
                    self.__audio,
                    self.__video,
                    self.__thread_quit,
                    self._throttle_playback,
                ),
            )
            self.__thread.start()

    def _stop(self, track: PlayerStreamTrack) -> None:
        self.__started.discard(track)

        if not self.__started and self.__thread is not None:
            self.__log_debug("Stopping worker thread")
            self.__thread_quit.set() # type: ignore
            self.__thread.join()
            self.__thread = None

        if not self.__started and self.__container is not None:
            self.__container.close()
            self.__container = None

    def __log_debug(self, msg: str, *args) -> None:
        logger.debug(f"MediaPlayer(%s) {msg}", self.__container.name, *args) # type: ignore


class MediaRecorderContext:
    def __init__(self, stream):
        self.stream = stream
        self.task = None


class MediaRecorder:
    """
    A media sink that writes audio and/or video to a file.

    Examples:

    .. code-block:: python

        # Write to a video file.
        player = MediaRecorder('/path/to/file.mp4')

        # Write to a set of images.
        player = MediaRecorder('/path/to/file-%3d.png')

    :param file: The path to a file, or a file-like object.
    :param format: The format to use, defaults to autodect.
    :param options: Additional options to pass to FFmpeg.
    """

    def __init__(self, file, format=None, options={}):
        self.__container = av.open(file=file, format=format, mode="w", options=options)
        self.__tracks = {}

    def addTrack(self, track):
        """
        Add a track to be recorded.

        :param track: A :class:`aiortc.MediaStreamTrack`.
        """
        if track.kind == "audio":
            if self.__container.format.name in ("wav", "alsa"): # type: ignore
                codec_name = "pcm_s16le"
            elif self.__container.format.name == "mp3": # type: ignore
                codec_name = "mp3"
            else:
                codec_name = "aac"
            stream = self.__container.add_stream(codec_name) # type: ignore
        else:
            if self.__container.format.name == "image2": # type: ignore
                stream = self.__container.add_stream("png", rate=30) # type: ignore
                stream.pix_fmt = "rgb24"
            else:
                stream = self.__container.add_stream("libx264", rate=30) # type: ignore
                stream.pix_fmt = "yuv420p"
        self.__tracks[track] = MediaRecorderContext(stream)

    async def start(self):
        """
        Start recording.
        """
        for track, context in self.__tracks.items():
            if context.task is None:
                context.task = asyncio.ensure_future(self.__run_track(track, context))

    async def stop(self):
        """
        Stop recording.
        """
        if self.__container:
            for track, context in self.__tracks.items():
                if context.task is not None:
                    context.task.cancel()
                    context.task = None
                    for packet in context.stream.encode(None):
                        self.__container.mux(packet)
            self.__tracks = {}

            if self.__container:
                self.__container.close()
                self.__container = None

    async def __run_track(self, track, context):
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            for packet in context.stream.encode(frame):
                self.__container.mux(packet) # type: ignore


class RelayStreamTrack(MediaStreamTrack):
    def __init__(self, relay, source: MediaStreamTrack) -> None:
        super().__init__()
        self.kind = source.kind
        self._relay = relay
        self._queue: asyncio.Queue[Optional[av.frame.Frame]] = asyncio.Queue() # type: ignore
        self._source: Optional[MediaStreamTrack] = source

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError

        self._relay._start(self) # type: ignore
        frame = await self._queue.get()
        if frame is None:
            self.stop()
            raise MediaStreamError
        return frame

    def stop(self):
        super().stop()
        if self._relay is not None:
            self._relay._stop(self)
            self._relay = None
            self._source = None


class MediaRelay:
    """
    A media source that relays one or more tracks to multiple consumers.

    This is especially useful for live tracks such as webcams or media received
    over the network.
    """

    def __init__(self) -> None:
        self.__proxies: Dict[MediaStreamTrack, Set[RelayStreamTrack]] = {}
        self.__tasks: Dict[MediaStreamTrack, asyncio.Future[None]] = {}

    def subscribe(self, track: MediaStreamTrack) -> MediaStreamTrack:
        """
        Create a proxy around the given `track` for a new consumer.
        """
        proxy = RelayStreamTrack(self, track)
        self.__log_debug("Create proxy %s for source %s", id(proxy), id(track))
        if track not in self.__proxies:
            self.__proxies[track] = set()
        return proxy

    def _start(self, proxy: RelayStreamTrack) -> None:
        track = proxy._source
        if track is not None and track in self.__proxies:
            # register proxy
            if proxy not in self.__proxies[track]:
                self.__log_debug("Start proxy %s", id(proxy))
                self.__proxies[track].add(proxy)

            # start worker
            if track not in self.__tasks:
                self.__tasks[track] = asyncio.ensure_future(self.__run_track(track))

    def _stop(self, proxy: RelayStreamTrack) -> None:
        track = proxy._source
        if track is not None and track in self.__proxies:
            # unregister proxy
            self.__log_debug("Stop proxy %s", id(proxy))
            self.__proxies[track].discard(proxy)

    def __log_debug(self, msg: str, *args) -> None:
        logger.debug(f"MediaRelay(%s) {msg}", id(self), *args)

    async def __run_track(self, track: MediaStreamTrack) -> None:
        self.__log_debug("Start reading source %s" % id(track))

        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                frame = None
            for proxy in self.__proxies[track]:
                proxy._queue.put_nowait(frame)
            if frame is None:
                break

        self.__log_debug("Stop reading source %s", id(track))
        del self.__proxies[track]
        del self.__tasks[track]
