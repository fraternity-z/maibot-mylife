"""联网计时的离线回归测试；所有外部时间源均使用 Mock。"""
import asyncio
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import struct
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


_SPEC=importlib.util.spec_from_file_location("mai_network_clock",Path(__file__).parents[1]/"core"/"network_clock.py")
clock_module=importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(clock_module)
NetworkClock=clock_module.NetworkClock


def _environment_module():
    root=Path(__file__).parents[1]
    package=ModuleType("mai_clock_test"); package.__path__=[str(root)]
    core=ModuleType("mai_clock_test.core"); core.__path__=[str(root/"core")]
    information=ModuleType("mai_clock_test.information"); information.__path__=[str(root/"information")]
    http=ModuleType("mai_clock_test.information.http_client"); http.HttpClient=Mock
    modules={"mai_clock_test":package,"mai_clock_test.core":core,"mai_clock_test.information":information,
             "mai_clock_test.core.network_clock":clock_module,"mai_clock_test.information.http_client":http}
    with patch.dict(sys.modules,modules):
        spec=importlib.util.spec_from_file_location("mai_clock_test.core.environment",root/"core"/"environment.py")
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


environment_module=_environment_module()


class NetworkClockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logger=Mock(); self.http=Mock(request=AsyncMock())
        self.clock=NetworkClock(self.logger,self.http)

    async def _ntp_query(self, change=lambda packet: None, timestamp=1_793_850_000):
        loop=Mock(getaddrinfo=AsyncMock(return_value=[(2,2,17,"",("203.0.113.1",123))]),
                  sock_connect=AsyncMock(),sock_sendall=AsyncMock())
        async def receive(*args):
            request=loop.sock_sendall.call_args.args[1]
            packet=bytearray(48); packet[0]=0x24; packet[1]=2
            packet[24:32]=request[40:48]
            for offset,stamp in ((32,timestamp),(40,timestamp+0.05)):
                ntp=stamp+clock_module._NTP_EPOCH
                struct.pack_into("!II",packet,offset,int(ntp)%2**32,int((ntp%1)*2**32))
            change(packet)
            return bytes(packet)
        loop.sock_recv=AsyncMock(side_effect=receive)
        with patch.object(clock_module.asyncio,"get_running_loop",return_value=loop), \
                patch.object(clock_module.socket,"socket"), \
                patch.object(clock_module,"time",return_value=0), \
                patch.object(clock_module,"monotonic",side_effect=[10,10.2]):
            return await self.clock._query_ntp("mock.ntp")

    async def test_ntp_corrects_wrong_system_clock_and_handles_2036_rollover(self):
        for timestamp in (1_793_850_000,2_208_988_800):
            with self.subTest(timestamp=timestamp):
                utc,measured_at=await self._ntp_query(timestamp=timestamp)
                self.assertAlmostEqual(utc,timestamp+0.125,places=5)
                self.assertEqual(measured_at,10.2)

    async def test_ntp_rejects_bad_or_unrelated_packets(self):
        changes=[
            lambda p:p.__delitem__(slice(20,None)),
            lambda p:p.__setitem__(0,0xE4),
            lambda p:p.__setitem__(0,0x14),
            lambda p:p.__setitem__(0,0x23),
            lambda p:p.__setitem__(1,0),
            lambda p:p.__setitem__(1,16),
            lambda p:p.__setitem__(slice(24,32),b"invalid!"),
            lambda p:p.__setitem__(slice(40,48),bytes(8)),
            lambda p:p.__setitem__(slice(32,40),bytes(8)),
            lambda p:p.__setitem__(slice(32,40),struct.pack("!II",4_002_838_801,0)),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                with self.assertRaises(ValueError):await self._ntp_query(change)

    async def test_fallback_source_and_monotonic_cache(self):
        with patch.object(self.clock,"_query_ntp",AsyncMock(side_effect=[OSError(),(1000,10)])) as query, \
                patch.object(clock_module,"monotonic",return_value=10):
            self.assertTrue(await self.clock.refresh())
            self.assertEqual(query.await_count,2)
            self.assertEqual(self.clock.source,clock_module._NTP_SERVERS[1])
            self.assertTrue(await self.clock.refresh())
            self.assertEqual(query.await_count,2)
        with patch.object(clock_module,"monotonic",return_value=25), \
                patch.object(clock_module,"time",return_value=-3600):
            self.assertEqual(self.clock.timestamp(),1015)

    async def test_failure_keeps_calibrated_time_and_throttles_retries(self):
        self.clock._anchor=(1000,10)
        with patch.object(self.clock,"_query_ntp",AsyncMock(side_effect=TimeoutError())) as ntp, \
                patch.object(self.clock,"_query_https",AsyncMock(side_effect=ValueError())), \
                patch.object(clock_module,"monotonic",return_value=50):
            self.assertFalse(await self.clock.refresh())
            self.assertEqual(self.clock.timestamp(),1040)
            self.assertTrue(await self.clock.refresh())
            self.assertEqual(ntp.await_count,3)
            self.assertFalse(await self.clock.refresh(force=True))
            self.assertEqual(ntp.await_count,6)
        self.logger.warning.assert_called_once()
        self.assertIn("上次校准时间",self.logger.warning.call_args.args[0])

    async def test_unsynchronized_failure_uses_system_utc_and_retries_after_delay(self):
        with patch.object(self.clock,"_query_ntp",AsyncMock(side_effect=OSError())) as ntp, \
                patch.object(self.clock,"_query_https",AsyncMock(side_effect=ValueError())), \
                patch.object(clock_module,"monotonic",return_value=50), \
                patch.object(clock_module,"time",return_value=123):
            self.assertFalse(await self.clock.refresh())
            self.assertFalse(await self.clock.refresh())
            self.assertEqual(ntp.await_count,3)
            self.assertEqual(self.clock.timestamp(),123)
        with patch.object(self.clock,"_query_ntp",AsyncMock(return_value=(1500,351))), \
                patch.object(clock_module,"monotonic",return_value=351):
            self.assertTrue(await self.clock.refresh())
            self.assertEqual(self.clock.timestamp(),1500)
        self.assertIn("尚未校准",self.logger.warning.call_args.args[0])

    async def test_https_fallback_and_date_validation(self):
        self.http.request.return_value=SimpleNamespace(status=200,headers={"date":"Thu, 05 Nov 2026 08:00:00 GMT"})
        with patch.object(self.clock,"_query_ntp",AsyncMock(side_effect=OSError())):
            self.assertTrue(await self.clock.refresh())
            self.assertEqual(self.clock.source,clock_module._HTTPS_TIME_URL)
        for headers in ({"age":"1","date":"Thu, 05 Nov 2026 08:00:00 GMT"},
                        {"age":"nan","date":"Thu, 05 Nov 2026 08:00:00 GMT"},
                        {"date":"not a date"},{"date":"Thu, 05 Nov 2026 08:00:00"},{}):
            self.http.request.return_value=SimpleNamespace(status=200,headers=headers)
            with self.subTest(headers=headers):
                with self.assertRaises((ValueError,TypeError)):await self.clock._query_https()

    async def test_unload_cancellation_is_not_swallowed(self):
        with patch.object(self.clock,"_query_ntp",AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):await self.clock.refresh()


class EnvironmentTimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config=SimpleNamespace(environment=SimpleNamespace(timezone="Asia/Shanghai",city="Shanghai"))
        self.logger=Mock()
        self.env=environment_module.EnvironmentService(Mock(),self.config,self.logger,http=Mock())

    async def test_network_utc_is_converted_to_shanghai_across_midnight(self):
        timestamp=datetime(2026,11,5,20,30,tzinfo=timezone.utc).timestamp()
        with patch.object(self.env.clock,"timestamp",return_value=timestamp), \
                patch.dict("os.environ",{"TZ":"UTC"}):
            self.assertEqual(self.env.now().isoformat(),"2026-11-06T04:30:00+08:00")
            self.assertEqual(self.env.snapshot()["date"],"2026-11-06")
            self.config.environment.timezone="UTC"
            self.assertEqual(self.env.now().isoformat(),"2026-11-05T20:30:00+00:00")

    async def test_missing_timezone_data_warns_once_and_preserves_network_time(self):
        with patch.object(environment_module,"ZoneInfo",side_effect=ValueError()), \
                patch.object(self.env.clock,"timestamp",return_value=0):
            self.assertEqual(self.env.now().isoformat(),"1970-01-01T08:00:00+08:00")
            self.env.now()
        self.logger.warning.assert_called_once()

    async def test_refresh_time_delegates_without_network_in_now(self):
        with patch.object(self.env.clock,"refresh",AsyncMock(return_value=True)) as refresh:
            self.assertTrue(await self.env.refresh_time(force=True))
            refresh.assert_awaited_once_with(force=True)
            self.env.now(); self.env.now()
            refresh.assert_awaited_once()

    async def test_weather_cache_uses_calibrated_time_and_replaces_future_cache(self):
        self.config.environment.weather_refresh_minutes=30
        cached={"fetched_at":990,"description":"cached","raw_json":{"_mai_life_query_city":"shanghai"}}
        self.env.store=Mock(get_weather=AsyncMock(return_value=cached),save_weather=AsyncMock())
        self.env._resolve_city=AsyncMock(return_value=("Shanghai",31,121))
        self.env.http=Mock(get=AsyncMock(return_value=SimpleNamespace(json=lambda:{"current":{"weather_code":0}})))
        with patch.object(self.env.clock,"timestamp",return_value=1000):
            self.assertEqual(await self.env.refresh_weather(),cached)
            self.env.http.get.assert_not_awaited()
            cached["fetched_at"]=1000+8*3600
            result=await self.env.refresh_weather()
            self.assertEqual(result["fetched_at"],1000)
            self.env.http.get.assert_awaited_once()
            self.env.store.save_weather.assert_awaited_once()


if __name__=="__main__":unittest.main()
