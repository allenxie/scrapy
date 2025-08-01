from __future__ import annotations

import asyncio
import contextlib
import logging
import pprint
import signal
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, TypeVar

from twisted.internet.defer import Deferred, DeferredList, inlineCallbacks

from scrapy import Spider, signals
from scrapy.addons import AddonManager
from scrapy.core.engine import ExecutionEngine
from scrapy.extension import ExtensionManager
from scrapy.settings import Settings, overridden_settings
from scrapy.signalmanager import SignalManager
from scrapy.spiderloader import SpiderLoaderProtocol, get_spider_loader
from scrapy.utils.asyncio import is_asyncio_available
from scrapy.utils.defer import deferred_from_coro, deferred_to_future
from scrapy.utils.log import (
    LogCounterHandler,
    configure_logging,
    get_scrapy_root_handler,
    install_scrapy_root_handler,
    log_reactor_info,
    log_scrapy_info,
)
from scrapy.utils.misc import build_from_crawler, load_object
from scrapy.utils.ossignal import install_shutdown_handlers, signal_names
from scrapy.utils.reactor import (
    _asyncio_reactor_path,
    install_reactor,
    is_asyncio_reactor_installed,
    is_reactor_installed,
    verify_installed_asyncio_event_loop,
    verify_installed_reactor,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Generator, Iterable

    from scrapy.logformatter import LogFormatter
    from scrapy.statscollectors import StatsCollector
    from scrapy.utils.request import RequestFingerprinterProtocol


logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class Crawler:
    def __init__(
        self,
        spidercls: type[Spider],
        settings: dict[str, Any] | Settings | None = None,
        init_reactor: bool = False,
    ):
        if isinstance(spidercls, Spider):
            raise ValueError("The spidercls argument must be a class, not an object")

        if isinstance(settings, dict) or settings is None:
            settings = Settings(settings)

        self.spidercls: type[Spider] = spidercls
        self.settings: Settings = settings.copy()
        self.spidercls.update_settings(self.settings)
        self._update_root_log_handler()

        self.addons: AddonManager = AddonManager(self)
        self.signals: SignalManager = SignalManager(self)

        self._init_reactor: bool = init_reactor
        self.crawling: bool = False
        self._started: bool = False

        self.extensions: ExtensionManager | None = None
        self.stats: StatsCollector | None = None
        self.logformatter: LogFormatter | None = None
        self.request_fingerprinter: RequestFingerprinterProtocol | None = None
        self.spider: Spider | None = None
        self.engine: ExecutionEngine | None = None

    def _update_root_log_handler(self) -> None:
        if get_scrapy_root_handler() is not None:
            # scrapy root handler already installed: update it with new settings
            install_scrapy_root_handler(self.settings)

    def _apply_settings(self) -> None:
        if self.settings.frozen:
            return

        self.addons.load_settings(self.settings)
        self.stats = load_object(self.settings["STATS_CLASS"])(self)

        handler = LogCounterHandler(self, level=self.settings.get("LOG_LEVEL"))
        logging.root.addHandler(handler)
        # lambda is assigned to Crawler attribute because this way it is not
        # garbage collected after leaving the scope
        self.__remove_handler = lambda: logging.root.removeHandler(handler)
        self.signals.connect(self.__remove_handler, signals.engine_stopped)

        lf_cls: type[LogFormatter] = load_object(self.settings["LOG_FORMATTER"])
        self.logformatter = lf_cls.from_crawler(self)

        self.request_fingerprinter = build_from_crawler(
            load_object(self.settings["REQUEST_FINGERPRINTER_CLASS"]),
            self,
        )

        reactor_class: str = self.settings["TWISTED_REACTOR"]
        event_loop: str = self.settings["ASYNCIO_EVENT_LOOP"]
        if self._init_reactor:
            # this needs to be done after the spider settings are merged,
            # but before something imports twisted.internet.reactor
            if reactor_class:
                install_reactor(reactor_class, event_loop)
            else:
                from twisted.internet import reactor  # noqa: F401
        if reactor_class:
            verify_installed_reactor(reactor_class)
            if is_asyncio_reactor_installed() and event_loop:
                verify_installed_asyncio_event_loop(event_loop)

        if self._init_reactor or reactor_class:
            log_reactor_info()

        self.extensions = ExtensionManager.from_crawler(self)
        self.settings.freeze()

        d = dict(overridden_settings(self.settings))
        logger.info(
            "Overridden settings:\n%(settings)s", {"settings": pprint.pformat(d)}
        )

    # Cannot use @deferred_f_from_coro_f because that relies on the reactor
    # being installed already, which is done within _apply_settings(), inside
    # this method.
    @inlineCallbacks
    def crawl(self, *args: Any, **kwargs: Any) -> Generator[Deferred[Any], Any, None]:
        """Start the crawler by instantiating its spider class with the given
        *args* and *kwargs* arguments, while setting the execution engine in
        motion. Should be called only once.

        Return a deferred that is fired when the crawl is finished.
        """
        if self.crawling:
            raise RuntimeError("Crawling already taking place")
        if self._started:
            raise RuntimeError(
                "Cannot run Crawler.crawl() more than once on the same instance."
            )
        self.crawling = self._started = True

        try:
            self.spider = self._create_spider(*args, **kwargs)
            self._apply_settings()
            self._update_root_log_handler()
            self.engine = self._create_engine()
            yield deferred_from_coro(self.engine.open_spider_async(self.spider))
            yield deferred_from_coro(self.engine.start_async())
        except Exception:
            self.crawling = False
            if self.engine is not None:
                yield self.engine.close()
            raise

    async def crawl_async(self, *args: Any, **kwargs: Any) -> None:
        """Start the crawler by instantiating its spider class with the given
        *args* and *kwargs* arguments, while setting the execution engine in
        motion. Should be called only once.

        .. versionadded:: VERSION

        Complete when the crawl is finished.

        This function requires
        :class:`~twisted.internet.asyncioreactor.AsyncioSelectorReactor` to be
        installed.
        """
        if not is_asyncio_available():
            raise RuntimeError("Crawler.crawl_async() requires AsyncioSelectorReactor.")
        if self.crawling:
            raise RuntimeError("Crawling already taking place")
        if self._started:
            raise RuntimeError(
                "Cannot run Crawler.crawl_async() more than once on the same instance."
            )
        self.crawling = self._started = True

        try:
            self.spider = self._create_spider(*args, **kwargs)
            self._apply_settings()
            self._update_root_log_handler()
            self.engine = self._create_engine()
            await self.engine.open_spider_async(self.spider)
            await self.engine.start_async()
        except Exception:
            self.crawling = False
            if self.engine is not None:
                await deferred_to_future(self.engine.close())
            raise

    def _create_spider(self, *args: Any, **kwargs: Any) -> Spider:
        return self.spidercls.from_crawler(self, *args, **kwargs)

    def _create_engine(self) -> ExecutionEngine:
        return ExecutionEngine(self, lambda _: self.stop())

    @inlineCallbacks
    def stop(self) -> Generator[Deferred[Any], Any, None]:
        """Start a graceful stop of the crawler and return a deferred that is
        fired when the crawler is stopped."""
        if self.crawling:
            self.crawling = False
            assert self.engine
            yield self.engine.stop()

    async def stop_async(self) -> None:
        """Start a graceful stop of the crawler and complete when the crawler is stopped.

        .. versionadded:: VERSION

        This function requires
        :class:`~twisted.internet.asyncioreactor.AsyncioSelectorReactor` to be
        installed.
        """
        if not is_asyncio_available():
            raise RuntimeError("Crawler.stop_async() requires AsyncioSelectorReactor.")
        await deferred_to_future(self.stop())

    @staticmethod
    def _get_component(
        component_class: type[_T], components: Iterable[Any]
    ) -> _T | None:
        for component in components:
            if isinstance(component, component_class):
                return component
        return None

    def get_addon(self, cls: type[_T]) -> _T | None:
        """Return the run-time instance of an :ref:`add-on <topics-addons>` of
        the specified class or a subclass, or ``None`` if none is found.

        .. versionadded:: 2.12
        """
        return self._get_component(cls, self.addons.addons)

    def get_downloader_middleware(self, cls: type[_T]) -> _T | None:
        """Return the run-time instance of a :ref:`downloader middleware
        <topics-downloader-middleware>` of the specified class or a subclass,
        or ``None`` if none is found.

        .. versionadded:: 2.12

        This method can only be called after the crawl engine has been created,
        e.g. at signals :signal:`engine_started` or :signal:`spider_opened`.
        """
        if not self.engine:
            raise RuntimeError(
                "Crawler.get_downloader_middleware() can only be called after "
                "the crawl engine has been created."
            )
        return self._get_component(cls, self.engine.downloader.middleware.middlewares)

    def get_extension(self, cls: type[_T]) -> _T | None:
        """Return the run-time instance of an :ref:`extension
        <topics-extensions>` of the specified class or a subclass,
        or ``None`` if none is found.

        .. versionadded:: 2.12

        This method can only be called after the extension manager has been
        created, e.g. at signals :signal:`engine_started` or
        :signal:`spider_opened`.
        """
        if not self.extensions:
            raise RuntimeError(
                "Crawler.get_extension() can only be called after the "
                "extension manager has been created."
            )
        return self._get_component(cls, self.extensions.middlewares)

    def get_item_pipeline(self, cls: type[_T]) -> _T | None:
        """Return the run-time instance of a :ref:`item pipeline
        <topics-item-pipeline>` of the specified class or a subclass, or
        ``None`` if none is found.

        .. versionadded:: 2.12

        This method can only be called after the crawl engine has been created,
        e.g. at signals :signal:`engine_started` or :signal:`spider_opened`.
        """
        if not self.engine:
            raise RuntimeError(
                "Crawler.get_item_pipeline() can only be called after the "
                "crawl engine has been created."
            )
        return self._get_component(cls, self.engine.scraper.itemproc.middlewares)

    def get_spider_middleware(self, cls: type[_T]) -> _T | None:
        """Return the run-time instance of a :ref:`spider middleware
        <topics-spider-middleware>` of the specified class or a subclass, or
        ``None`` if none is found.

        .. versionadded:: 2.12

        This method can only be called after the crawl engine has been created,
        e.g. at signals :signal:`engine_started` or :signal:`spider_opened`.
        """
        if not self.engine:
            raise RuntimeError(
                "Crawler.get_spider_middleware() can only be called after the "
                "crawl engine has been created."
            )
        return self._get_component(cls, self.engine.scraper.spidermw.middlewares)


class CrawlerRunnerBase(ABC):
    def __init__(self, settings: dict[str, Any] | Settings | None = None):
        if isinstance(settings, dict) or settings is None:
            settings = Settings(settings)
        AddonManager.load_pre_crawler_settings(settings)
        self.settings: Settings = settings
        self.spider_loader: SpiderLoaderProtocol = get_spider_loader(settings)
        self._crawlers: set[Crawler] = set()
        self.bootstrap_failed = False

    @property
    def crawlers(self) -> set[Crawler]:
        """Set of :class:`crawlers <scrapy.crawler.Crawler>` started by
        :meth:`crawl` and managed by this class."""
        return self._crawlers

    def create_crawler(
        self, crawler_or_spidercls: type[Spider] | str | Crawler
    ) -> Crawler:
        """
        Return a :class:`~scrapy.crawler.Crawler` object.

        * If ``crawler_or_spidercls`` is a Crawler, it is returned as-is.
        * If ``crawler_or_spidercls`` is a Spider subclass, a new Crawler
          is constructed for it.
        * If ``crawler_or_spidercls`` is a string, this function finds
          a spider with this name in a Scrapy project (using spider loader),
          then creates a Crawler instance for it.
        """
        if isinstance(crawler_or_spidercls, Spider):
            raise ValueError(
                "The crawler_or_spidercls argument cannot be a spider object, "
                "it must be a spider class (or a Crawler object)"
            )
        if isinstance(crawler_or_spidercls, Crawler):
            return crawler_or_spidercls
        return self._create_crawler(crawler_or_spidercls)

    def _create_crawler(self, spidercls: str | type[Spider]) -> Crawler:
        if isinstance(spidercls, str):
            spidercls = self.spider_loader.load(spidercls)
        return Crawler(spidercls, self.settings)

    @abstractmethod
    def crawl(
        self,
        crawler_or_spidercls: type[Spider] | str | Crawler,
        *args: Any,
        **kwargs: Any,
    ) -> Awaitable[None]:
        raise NotImplementedError


class CrawlerRunner(CrawlerRunnerBase):
    """
    This is a convenient helper class that keeps track of, manages and runs
    crawlers inside an already setup :mod:`~twisted.internet.reactor`.

    The CrawlerRunner object must be instantiated with a
    :class:`~scrapy.settings.Settings` object.

    This class shouldn't be needed (since Scrapy is responsible of using it
    accordingly) unless writing scripts that manually handle the crawling
    process. See :ref:`run-from-script` for an example.

    This class provides Deferred-based APIs. Use :class:`AsyncCrawlerRunner`
    for modern coroutine APIs.
    """

    def __init__(self, settings: dict[str, Any] | Settings | None = None):
        super().__init__(settings)
        self._active: set[Deferred[None]] = set()

    def crawl(
        self,
        crawler_or_spidercls: type[Spider] | str | Crawler,
        *args: Any,
        **kwargs: Any,
    ) -> Deferred[None]:
        """
        Run a crawler with the provided arguments.

        It will call the given Crawler's :meth:`~Crawler.crawl` method, while
        keeping track of it so it can be stopped later.

        If ``crawler_or_spidercls`` isn't a :class:`~scrapy.crawler.Crawler`
        instance, this method will try to create one using this parameter as
        the spider class given to it.

        Returns a deferred that is fired when the crawling is finished.

        :param crawler_or_spidercls: already created crawler, or a spider class
            or spider's name inside the project to create it
        :type crawler_or_spidercls: :class:`~scrapy.crawler.Crawler` instance,
            :class:`~scrapy.spiders.Spider` subclass or string

        :param args: arguments to initialize the spider

        :param kwargs: keyword arguments to initialize the spider
        """
        if isinstance(crawler_or_spidercls, Spider):
            raise ValueError(
                "The crawler_or_spidercls argument cannot be a spider object, "
                "it must be a spider class (or a Crawler object)"
            )
        crawler = self.create_crawler(crawler_or_spidercls)
        return self._crawl(crawler, *args, **kwargs)

    @inlineCallbacks
    def _crawl(
        self, crawler: Crawler, *args: Any, **kwargs: Any
    ) -> Generator[Deferred[Any], Any, None]:
        self.crawlers.add(crawler)
        d = crawler.crawl(*args, **kwargs)
        self._active.add(d)
        try:
            yield d
        finally:
            self.crawlers.discard(crawler)
            self._active.discard(d)
            self.bootstrap_failed |= not getattr(crawler, "spider", None)

    def stop(self) -> Deferred[Any]:
        """
        Stops simultaneously all the crawling jobs taking place.

        Returns a deferred that is fired when they all have ended.
        """
        return DeferredList(c.stop() for c in self.crawlers)

    @inlineCallbacks
    def join(self) -> Generator[Deferred[Any], Any, None]:
        """
        join()

        Returns a deferred that is fired when all managed :attr:`crawlers` have
        completed their executions.
        """
        while self._active:
            yield DeferredList(self._active)


class AsyncCrawlerRunner(CrawlerRunnerBase):
    """
    This is a convenient helper class that keeps track of, manages and runs
    crawlers inside an already setup :mod:`~twisted.internet.reactor`.

    The AsyncCrawlerRunner object must be instantiated with a
    :class:`~scrapy.settings.Settings` object.

    This class shouldn't be needed (since Scrapy is responsible of using it
    accordingly) unless writing scripts that manually handle the crawling
    process. See :ref:`run-from-script` for an example.

    This class provides coroutine APIs. It requires
    :class:`~twisted.internet.asyncioreactor.AsyncioSelectorReactor`.
    """

    def __init__(self, settings: dict[str, Any] | Settings | None = None):
        super().__init__(settings)
        self._active: set[asyncio.Task[None]] = set()

    def crawl(
        self,
        crawler_or_spidercls: type[Spider] | str | Crawler,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Task[None]:
        """
        Run a crawler with the provided arguments.

        It will call the given Crawler's :meth:`~Crawler.crawl` method, while
        keeping track of it so it can be stopped later.

        If ``crawler_or_spidercls`` isn't a :class:`~scrapy.crawler.Crawler`
        instance, this method will try to create one using this parameter as
        the spider class given to it.

        Returns a :class:`~asyncio.Task` object which completes when the
        crawling is finished.

        :param crawler_or_spidercls: already created crawler, or a spider class
            or spider's name inside the project to create it
        :type crawler_or_spidercls: :class:`~scrapy.crawler.Crawler` instance,
            :class:`~scrapy.spiders.Spider` subclass or string

        :param args: arguments to initialize the spider

        :param kwargs: keyword arguments to initialize the spider
        """
        if isinstance(crawler_or_spidercls, Spider):
            raise ValueError(
                "The crawler_or_spidercls argument cannot be a spider object, "
                "it must be a spider class (or a Crawler object)"
            )
        if not is_asyncio_reactor_installed():
            raise RuntimeError(
                f"{type(self).__name__} requires AsyncioSelectorReactor."
            )
        crawler = self.create_crawler(crawler_or_spidercls)
        return self._crawl(crawler, *args, **kwargs)

    def _crawl(self, crawler: Crawler, *args: Any, **kwargs: Any) -> asyncio.Task[None]:
        # At this point the asyncio loop has been installed either by the user
        # or by AsyncCrawlerProcess (but it isn't running yet, so no asyncio.create_task()).
        loop = asyncio.get_event_loop()
        self.crawlers.add(crawler)
        task = loop.create_task(crawler.crawl_async(*args, **kwargs))
        self._active.add(task)

        def _done(_: asyncio.Task[None]) -> None:
            self.crawlers.discard(crawler)
            self._active.discard(task)
            self.bootstrap_failed |= not getattr(crawler, "spider", None)

        task.add_done_callback(_done)
        return task

    async def stop(self) -> None:
        """
        Stops simultaneously all the crawling jobs taking place.

        Completes when they all have ended.
        """
        if self.crawlers:
            await asyncio.wait(
                [asyncio.create_task(c.stop_async()) for c in self.crawlers]
            )

    async def join(self) -> None:
        """
        Completes when all managed :attr:`crawlers` have completed their
        executions.
        """
        while self._active:
            await asyncio.wait(self._active)


class CrawlerProcessBase(CrawlerRunnerBase):
    def __init__(
        self,
        settings: dict[str, Any] | Settings | None = None,
        install_root_handler: bool = True,
    ):
        super().__init__(settings)
        configure_logging(self.settings, install_root_handler)
        log_scrapy_info(self.settings)

    @abstractmethod
    def start(
        self, stop_after_crawl: bool = True, install_signal_handlers: bool = True
    ) -> None:
        raise NotImplementedError

    def _signal_shutdown(self, signum: int, _: Any) -> None:
        from twisted.internet import reactor

        install_shutdown_handlers(self._signal_kill)
        signame = signal_names[signum]
        logger.info(
            "Received %(signame)s, shutting down gracefully. Send again to force ",
            {"signame": signame},
        )
        reactor.callFromThread(self._graceful_stop_reactor)

    def _signal_kill(self, signum: int, _: Any) -> None:
        from twisted.internet import reactor

        install_shutdown_handlers(signal.SIG_IGN)
        signame = signal_names[signum]
        logger.info(
            "Received %(signame)s twice, forcing unclean shutdown", {"signame": signame}
        )
        reactor.callFromThread(self._stop_reactor)

    def _setup_reactor(self, install_signal_handlers: bool) -> None:
        from twisted.internet import reactor

        resolver_class = load_object(self.settings["DNS_RESOLVER"])
        # We pass self, which is CrawlerProcess, instead of Crawler here,
        # which works because the default resolvers only use crawler.settings.
        resolver = build_from_crawler(resolver_class, self, reactor=reactor)  # type: ignore[arg-type]
        resolver.install_on_reactor()
        tp = reactor.getThreadPool()
        tp.adjustPoolsize(maxthreads=self.settings.getint("REACTOR_THREADPOOL_MAXSIZE"))
        reactor.addSystemEventTrigger("before", "shutdown", self._stop_dfd)
        if install_signal_handlers:
            reactor.addSystemEventTrigger(
                "after", "startup", install_shutdown_handlers, self._signal_shutdown
            )

    @abstractmethod
    def _stop_dfd(self) -> Deferred[Any]:
        raise NotImplementedError

    @inlineCallbacks
    def _graceful_stop_reactor(self) -> Generator[Deferred[Any], Any, None]:
        try:
            yield self._stop_dfd()
        finally:
            self._stop_reactor()

    def _stop_reactor(self, _: Any = None) -> None:
        from twisted.internet import reactor

        # raised if already stopped or in shutdown stage
        with contextlib.suppress(RuntimeError):
            reactor.stop()


class CrawlerProcess(CrawlerProcessBase, CrawlerRunner):
    """
    A class to run multiple scrapy crawlers in a process simultaneously.

    This class extends :class:`~scrapy.crawler.CrawlerRunner` by adding support
    for starting a :mod:`~twisted.internet.reactor` and handling shutdown
    signals, like the keyboard interrupt command Ctrl-C. It also configures
    top-level logging.

    This utility should be a better fit than
    :class:`~scrapy.crawler.CrawlerRunner` if you aren't running another
    :mod:`~twisted.internet.reactor` within your application.

    The CrawlerProcess object must be instantiated with a
    :class:`~scrapy.settings.Settings` object.

    :param install_root_handler: whether to install root logging handler
        (default: True)

    This class shouldn't be needed (since Scrapy is responsible of using it
    accordingly) unless writing scripts that manually handle the crawling
    process. See :ref:`run-from-script` for an example.

    This class provides Deferred-based APIs. Use :class:`AsyncCrawlerProcess`
    for modern coroutine APIs.
    """

    def __init__(
        self,
        settings: dict[str, Any] | Settings | None = None,
        install_root_handler: bool = True,
    ):
        super().__init__(settings, install_root_handler)
        self._initialized_reactor: bool = False

    def _create_crawler(self, spidercls: type[Spider] | str) -> Crawler:
        if isinstance(spidercls, str):
            spidercls = self.spider_loader.load(spidercls)
        init_reactor = not self._initialized_reactor
        self._initialized_reactor = True
        return Crawler(spidercls, self.settings, init_reactor=init_reactor)

    def _stop_dfd(self) -> Deferred[Any]:
        return self.stop()

    def start(
        self, stop_after_crawl: bool = True, install_signal_handlers: bool = True
    ) -> None:
        """
        This method starts a :mod:`~twisted.internet.reactor`, adjusts its pool
        size to :setting:`REACTOR_THREADPOOL_MAXSIZE`, and installs a DNS cache
        based on :setting:`DNSCACHE_ENABLED` and :setting:`DNSCACHE_SIZE`.

        If ``stop_after_crawl`` is True, the reactor will be stopped after all
        crawlers have finished, using :meth:`join`.

        :param bool stop_after_crawl: stop or not the reactor when all
            crawlers have finished

        :param bool install_signal_handlers: whether to install the OS signal
            handlers from Twisted and Scrapy (default: True)
        """
        from twisted.internet import reactor

        if stop_after_crawl:
            d = self.join()
            # Don't start the reactor if the deferreds are already fired
            if d.called:
                return
            d.addBoth(self._stop_reactor)

        self._setup_reactor(install_signal_handlers)
        reactor.run(installSignalHandlers=install_signal_handlers)  # blocking call


class AsyncCrawlerProcess(CrawlerProcessBase, AsyncCrawlerRunner):
    """
    A class to run multiple scrapy crawlers in a process simultaneously.

    This class extends :class:`~scrapy.crawler.AsyncCrawlerRunner` by adding support
    for starting a :mod:`~twisted.internet.reactor` and handling shutdown
    signals, like the keyboard interrupt command Ctrl-C. It also configures
    top-level logging.

    This utility should be a better fit than
    :class:`~scrapy.crawler.AsyncCrawlerRunner` if you aren't running another
    :mod:`~twisted.internet.reactor` within your application.

    The AsyncCrawlerProcess object must be instantiated with a
    :class:`~scrapy.settings.Settings` object.

    :param install_root_handler: whether to install root logging handler
        (default: True)

    This class shouldn't be needed (since Scrapy is responsible of using it
    accordingly) unless writing scripts that manually handle the crawling
    process. See :ref:`run-from-script` for an example.

    This class provides coroutine APIs. It requires
    :class:`~twisted.internet.asyncioreactor.AsyncioSelectorReactor`.
    """

    def __init__(
        self,
        settings: dict[str, Any] | Settings | None = None,
        install_root_handler: bool = True,
    ):
        super().__init__(settings, install_root_handler)
        # We want the asyncio event loop to be installed early, so that it's
        # always the correct one. And as we do that, we can also install the
        # reactor here.
        # The ASYNCIO_EVENT_LOOP setting cannot be overridden by add-ons and
        # spiders when using AsyncCrawlerProcess.
        loop_path = self.settings["ASYNCIO_EVENT_LOOP"]
        if is_reactor_installed():
            # The user could install a reactor before this class is instantiated.
            # We need to make sure the reactor is the correct one and the loop
            # type matches the setting.
            verify_installed_reactor(_asyncio_reactor_path)
            if loop_path:
                verify_installed_asyncio_event_loop(loop_path)
        else:
            install_reactor(_asyncio_reactor_path, loop_path)
        self._initialized_reactor = True

    def _stop_dfd(self) -> Deferred[Any]:
        return deferred_from_coro(self.stop())

    def start(
        self, stop_after_crawl: bool = True, install_signal_handlers: bool = True
    ) -> None:
        """
        This method starts a :mod:`~twisted.internet.reactor`, adjusts its pool
        size to :setting:`REACTOR_THREADPOOL_MAXSIZE`, and installs a DNS cache
        based on :setting:`DNSCACHE_ENABLED` and :setting:`DNSCACHE_SIZE`.

        If ``stop_after_crawl`` is True, the reactor will be stopped after all
        crawlers have finished, using :meth:`join`.

        :param bool stop_after_crawl: stop or not the reactor when all
            crawlers have finished

        :param bool install_signal_handlers: whether to install the OS signal
            handlers from Twisted and Scrapy (default: True)
        """
        from twisted.internet import reactor

        if stop_after_crawl:
            loop = asyncio.get_event_loop()
            join_task = loop.create_task(self.join())
            join_task.add_done_callback(self._stop_reactor)

        self._setup_reactor(install_signal_handlers)
        reactor.run(installSignalHandlers=install_signal_handlers)  # blocking call
