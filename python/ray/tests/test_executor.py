import os
import sys
import pytest
from ray.util.concurrent.futures.ray_executor import RayExecutor, _RoundRobinActorPool
import time
import ray
from ray.util.state import list_actors
from concurrent.futures import (
    ThreadPoolExecutor,
    ProcessPoolExecutor,
    TimeoutError as ConTimeoutError,
)
from concurrent.futures.thread import BrokenThreadPool
from ray.exceptions import RayTaskError, RayActorError

from ray._private.worker import RayContext


# ProcessPoolExecutor uses pickle which can only serialize top-level functions
def f_process1(x):
    return len([i for i in range(x) if i % 2 == 0])

class TestInitializerException(Exception):
    pass

def unsafe(exc):
    raise exc

def safe(_):
    pass


class TestShared:

    # This class is for tests that do not need to be run with isolated ray instances

    def get_actor_states(self, actor_pool):
        actor_ids = [i["actor"]._ray_actor_id.hex() for i in actor_pool.pool.values()]
        return [actor_state["state"] for actor_state in list_actors() if actor_state.actor_id in actor_ids]

    def get_actor_state(self, actor):
        [actor_state] = [actor_state["state"] for actor_state in list_actors() if actor_state.actor_id == actor._ray_actor_id.hex()]
        return actor_state

    def wait_actor_state(self, actor_pool, state, timeout = 20):
        while timeout > 0:
            states = self.get_actor_states(actor_pool)
            if not all(i == state for i in states):
                time.sleep(1)
                timeout -= 1
            else:
                break
        if timeout == 0:
            return False
        else:
            return True

    def wait_actor_state_(self, actor, expected_state, timeout = 20):
        while timeout > 0:
            state = self.get_actor_state(actor)
            if state != expected_state:
                time.sleep(1)
                timeout -= 1
            else:
                break
        if timeout == 0:
            return False
        else:
            return True

    def test_remote_function_runs_on_specified_instance(self, call_ray_start):
        with RayExecutor(address=call_ray_start) as ex:
            result = ex.submit(lambda x: x * x, 100).result()
            assert result == 10_000
            assert ex._context is not None
            assert type(ex._context) == RayContext
            assert ex._context.address_info["address"] == call_ray_start

    def test_remote_function_runs_on_specified_instance_with_map(self, call_ray_start):
        with RayExecutor(address=call_ray_start) as ex:
            futures_iter = ex.map(lambda x: x * x, [100, 100, 100])
            for result in futures_iter:
                assert result == 10_000
            assert ex._context is not None
            assert type(ex._context) == RayContext
            assert ex._context.address_info["address"] == call_ray_start

    def test_round_robin_actor_pool_must_have_actor(self):
        with pytest.raises(ValueError):
            _RoundRobinActorPool(num_actors=0)

    def test_round_robin_actor_pool_cycles_through_actors(self, call_ray_start):
        pool = _RoundRobinActorPool(num_actors=2)
        assert len(pool.pool) == 2
        assert pool.index == 0
        _ = pool.next()
        assert len(pool.pool) == 2
        assert pool.index == 1
        _ = pool.next()
        assert len(pool.pool) == 2
        assert pool.index == 0

    def test_round_robin_actor_pool_kills_actors(self, call_ray_start):
        pool = _RoundRobinActorPool(num_actors=2)
        assert len(pool.pool) == 2
        assert pool.index == 0

        # wait for actors to live
        assert self.wait_actor_state(pool, "ALIVE") == True
        pool.kill()
        # wait for actors to die
        assert self.wait_actor_state(pool, "DEAD") == True

    def test_round_robin_actor_pool_kills_actors_and_does_not_wait_for_tasks_to_complete(self, call_ray_start):
        pool = _RoundRobinActorPool(num_actors=2)

        def f():
            return 123

        future = pool.submit(f)
        pool.kill()
        assert self.wait_actor_state(pool, "DEAD") == True
        with pytest.raises(RayActorError):
            future.result()

    def test_round_robin_actor_pool_exits_actors_and_waits_for_tasks_to_complete(self, call_ray_start):
        pool = _RoundRobinActorPool(num_actors=2)

        def f():
            return 123

        future = pool.submit(f)
        actor = pool._exit_actor(0)
        assert self.wait_actor_state_(actor, "DEAD") == True
        assert future.result() == 123

    def test_round_robin_actor_pool_replaces_expired_actors(self, call_ray_start):
        pool = _RoundRobinActorPool(num_actors=2, max_tasks_per_actor=2)
        assert pool.index == 0
        actor_id0 = pool.pool[0]["actor"]._ray_actor_id.hex()
        pool._replace_actor_if_max_tasks()
        assert pool.pool[0]["actor"]._ray_actor_id.hex() == actor_id0
        pool.pool[0]["task_count"] = 2
        pool._replace_actor_if_max_tasks()
        assert pool.pool[0]["actor"]._ray_actor_id.hex() != actor_id0

    def test_round_robin_actor_pool_replaces_actors_allowing_tasks_to_finish(self, call_ray_start):

        def f():
            return 123

        pool = _RoundRobinActorPool(num_actors=2, max_tasks_per_actor=2)
        assert pool.index == 0

        actor_id00 = pool.pool[0]["actor"]._ray_actor_id.hex()
        future0 = pool.submit(f)

        assert pool.index == 1
        future1 = pool.submit(f)

        assert pool.index == 0
        pool.submit(f)

        assert pool.index == 1
        pool.submit(f)

        actor_id01 = pool.pool[0]["actor"]._ray_actor_id.hex()
        assert pool.index == 0
        pool.submit(f)
        actor_id02 = pool.pool[0]["actor"]._ray_actor_id.hex()

        assert pool.index == 1
        assert actor_id00 == actor_id01
        assert actor_id01 != actor_id02

        assert future0.result() == 123
        assert future1.result() == 123

    def test_round_robin_actor_pool_replaces_actors_exits_gracefully(self, call_ray_start):
        def f():
            time.sleep(5)
            return 123

        pool = _RoundRobinActorPool(num_actors=1, max_tasks_per_actor=1)
        assert pool.index == 0
        actor_id00 = pool.pool[0]["actor"]._ray_actor_id.hex()
        future0 = pool.submit(f)
        actor_id01 = pool.pool[0]["actor"]._ray_actor_id.hex()
        future1 = pool.submit(f)
        actor_id02 = pool.pool[0]["actor"]._ray_actor_id.hex()
        assert actor_id00 == actor_id01
        assert actor_id01 != actor_id02
        assert 123 == future0.result()
        assert 123 == future1.result()

    def test_round_robin_actor_pool_replaces_actors_exits_gracefully_in_executor(self, call_ray_start):
        def f():
            time.sleep(5)
            return 123

        with RayExecutor(address=call_ray_start, max_workers=1, max_tasks_per_child=1) as ex:
            pool = ex.actor_pool
            assert pool.index == 0
            actor_id00 = pool.pool[0]["actor"]._ray_actor_id.hex()
            future0 = pool.submit(f)
            actor_id01 = pool.pool[0]["actor"]._ray_actor_id.hex()
            future1 = pool.submit(f)
            actor_id02 = pool.pool[0]["actor"]._ray_actor_id.hex()
            assert actor_id00 == actor_id01
            assert actor_id01 != actor_id02
            assert 123 == future0.result()
            assert 123 == future1.result()

    def test_round_robin_actor_pool_replaces_actors_exits_gracefully_in_executor2(self, call_ray_start):
        def f():
            time.sleep(5)
            return 123

        with RayExecutor(address=call_ray_start, max_workers=1, max_tasks_per_child=1) as ex:
            pool = ex.actor_pool
            assert pool.index == 0
            actor_id00 = pool.pool[0]["actor"]._ray_actor_id.hex()
            future0 = ex.submit(f)
            actor_id01 = pool.pool[0]["actor"]._ray_actor_id.hex()
            future1 = ex.submit(f)
            actor_id02 = pool.pool[0]["actor"]._ray_actor_id.hex()
            assert actor_id00 == actor_id01
            assert actor_id01 != actor_id02
            assert 123 == future0.result()
            assert 123 == future1.result()


#----------------------------------------------------------------------------------------------------



class TestIsolated:

    # This class is for tests that must be run with dedicated/isolated ray
    # instances. It forces tests to be run in series and the individual test is
    # responsible for creating its own ray instances.

    @pytest.fixture(autouse=True)
    def _tear_down(self):
        yield None
        ray.shutdown()

    def test_remote_function_runs_on_local_instance(self):
        with RayExecutor() as ex:
            result = ex.submit(lambda x: x * x, 100).result()
            assert result == 10_000

    def test_reuse_existing_cluster(self):
        with RayExecutor() as ex0:
            c0 = ray.runtime_context.get_runtime_context()
            n0 = c0.get_node_id()
            with RayExecutor() as ex1:
                c1 = ray.runtime_context.get_runtime_context()
                n1 = c1.get_node_id()
                assert n0 == n1
                assert ex0._context is not None
                assert ex1._context is not None
                assert type(ex0._context) == RayContext
                assert type(ex1._context) == RayContext
                assert (
                    ex0._context.address_info["node_id"]
                    == ex1._context.address_info["node_id"]
                )

    def test_existing_instance_ignores_max_workers(self):
        _ = ray.init(num_cpus=1)
        with RayExecutor(max_workers=2):
            assert ray.available_resources()["CPU"] == 1

    def test_remote_function_runs_multiple_tasks_on_local_instance(self):
        with RayExecutor() as ex:
            result0 = ex.submit(lambda x: x * x, 100).result()
            result1 = ex.submit(lambda x: x * x, 100).result()
            assert result0 == result1 == 10_000

    def test_order_retained(self):
        def f(x, y):
            return x * y

        with RayExecutor() as ex:
            r0 = list(ex.map(f, [100, 100, 100], [1, 2, 3]))
        with RayExecutor(max_workers=2) as ex:
            r1 = list(ex.map(f, [100, 100, 100], [1, 2, 3]))
        assert r0 == r1

    def test_remote_function_runs_on_local_instance_with_map(self):
        with RayExecutor() as ex:
            futures_iter = ex.map(lambda x: x * x, [100, 100, 100])
            for result in futures_iter:
                assert result == 10_000

    def test_map_zips_iterables(self):
        def f(x, y):
            return x * y

        with RayExecutor() as ex:
            futures_iter = ex.map(f, [100, 100, 100], [1, 2, 3])
            assert list(futures_iter) == [100, 200, 300]

    def test_remote_function_map_using_max_workers(self):
        with RayExecutor(max_workers=3) as ex:
            assert ex.actor_pool is not None
            assert len(ex.actor_pool.pool) == 3
            time_start = time.monotonic()
            _ = list(ex.map(lambda _: time.sleep(1), range(12)))
            time_end = time.monotonic()
            # we expect about (12*1) / 3 = 4 rounds
            delta = time_end - time_start
            assert delta > 3.0

    def test_results_are_not_accessible_after_shutdown(self):
        # note: this will hang indefinitely if no timeout is specified on map()
        def f(x, y):
            return x * y
        with RayExecutor() as ex:
            r1 = ex.map(f, [100, 100, 100], [1, 2, 3], timeout=2)
        assert ex._shutdown_lock
        with pytest.raises(ConTimeoutError):
            _ = list(r1)

    def test_remote_function_max_workers_same_result(self):
        with RayExecutor() as ex:
            f0 = list(ex.map(lambda x: x * x, range(12)))
        with RayExecutor(max_workers=1) as ex:
            f1 = list(ex.map(lambda x: x * x, range(12)))
        with RayExecutor(max_workers=3) as ex:
            f3 = list(ex.map(lambda x: x * x, range(12)))
        assert f0 == f1 == f3

    def test_map_times_out(self):
        def f(x):
            time.sleep(2)
            return x

        with RayExecutor() as ex:
            with pytest.raises(ConTimeoutError):
                i1 = ex.map(f, [1, 2, 3], timeout=1)
                for _ in i1:
                    pass

    def test_map_times_out_with_max_workers(self):
        def f(x):
            time.sleep(2)
            return x

        with RayExecutor(max_workers=2) as ex:
            with pytest.raises(ConTimeoutError):
                i1 = ex.map(f, [1, 2, 3], timeout=1)
                for _ in i1:
                    pass

    def test_remote_function_runs_multiple_tasks_using_max_workers(self):
        with RayExecutor(max_workers=2) as ex:
            result0 = ex.submit(lambda x: x * x, 100).result()
            result1 = ex.submit(lambda x: x * x, 100).result()
            assert result0 == result1 == 10_000

    def test_cannot_submit_after_shutdown(self):
        ex = RayExecutor()
        ex.submit(lambda: True).result()
        ex.shutdown()
        with pytest.raises(RuntimeError):
            ex.submit(lambda: True).result()

    def test_can_submit_after_shutdown(self):
        ex = RayExecutor(shutdown_ray=False)
        ex.submit(lambda: True).result()
        ex.shutdown()
        try:
            ex.submit(lambda: True).result()
        except RuntimeError:
            assert (
                False
            ), "Could not submit after calling shutdown() with shutdown_ray=False"
        ex.shutdown_ray = True
        ex.shutdown()

    def test_cannot_map_after_shutdown(self):
        ex = RayExecutor()
        ex.submit(lambda: True).result()
        ex.shutdown()
        with pytest.raises(RuntimeError):
            ex.submit(lambda: True).result()

    def test_pending_task_is_cancelled_after_shutdown(self):
        ex = RayExecutor()
        f = ex.submit(lambda: True)
        assert f._state == "PENDING"
        ex.shutdown(cancel_futures=True)
        assert f.cancelled()

    def test_running_task_finishes_after_shutdown(self):
        ex = RayExecutor()
        f = ex.submit(lambda: True)
        assert f._state == "PENDING"
        f.set_running_or_notify_cancel()
        assert f.running()
        ex.shutdown(cancel_futures=True)
        assert f._state == "FINISHED"

    def test_mixed_task_states_handled_by_shutdown(self):
        ex = RayExecutor()
        f0 = ex.submit(lambda: True)
        f1 = ex.submit(lambda: True)
        assert f0._state == "PENDING"
        assert f1._state == "PENDING"
        f0.set_running_or_notify_cancel()
        ex.shutdown(cancel_futures=True)
        assert f0._state == "FINISHED"
        assert f1.cancelled()

    def test_with_syntax_invokes_shutdown(self):
        with RayExecutor() as ex:
            pass
        assert ex._shutdown_lock

    # ----------------------------------------------------------------------------------------------------
    # ThreadPool/ProcessPool comparison
    # ----------------------------------------------------------------------------------------------------

    def test_conformity_with_processpool(self):
        def f_process0(x):
            return len([i for i in range(x) if i % 2 == 0])

        assert f_process0.__code__.co_code == f_process1.__code__.co_code

        with RayExecutor() as ex:
            ray_future = ex.submit(f_process0, 100)
            ray_future_type = type(ray_future)
            ray_result = ray_future.result()
        with ProcessPoolExecutor() as ppe:
            ppe_future = ppe.submit(f_process1, 100)
            ppe_future_type = type(ppe_future)
            ppe_result = ppe_future.result()
        assert ray_future_type == ppe_future_type
        assert ray_result == ppe_result

    def test_conformity_with_processpool_map(self):
        def f_process0(x):
            return len([i for i in range(x) if i % 2 == 0])

        assert f_process0.__code__.co_code == f_process1.__code__.co_code

        with RayExecutor() as ex:
            ray_iter = ex.map(f_process0, range(10))
            ray_result = list(ray_iter)
        with ProcessPoolExecutor() as ppe:
            ppe_iter = ppe.map(f_process1, range(10))
            ppe_result = list(ppe_iter)
        assert hasattr(ray_iter, "__iter__")
        assert hasattr(ray_iter, "__next__")
        assert hasattr(ppe_iter, "__iter__")
        assert hasattr(ppe_iter, "__next__")
        assert type(ray_result) == type(ppe_result)
        assert sorted(ray_result) == sorted(ppe_result)

    def test_conformity_with_threadpool(self):
        def f_process0(x):
            return len([i for i in range(x) if i % 2 == 0])

        assert f_process0.__code__.co_code == f_process1.__code__.co_code

        with RayExecutor() as ex:
            ray_future = ex.submit(f_process0, 100)
            ray_future_type = type(ray_future)
            ray_result = ray_future.result()
        with ThreadPoolExecutor() as tpe:
            tpe_future = tpe.submit(f_process1, 100)
            tpe_future_type = type(tpe_future)
            tpe_result = tpe_future.result()
        assert ray_future_type == tpe_future_type
        assert ray_result == tpe_result

    def test_conformity_with_threadpool_map(self):
        def f_process0(x):
            return len([i for i in range(x) if i % 2 == 0])

        assert f_process0.__code__.co_code == f_process1.__code__.co_code

        with RayExecutor() as ex:
            ray_iter = ex.map(f_process0, range(10))
            ray_result = list(ray_iter)
        with ThreadPoolExecutor() as tpe:
            tpe_iter = tpe.map(f_process1, range(10))
            tpe_result = list(tpe_iter)
        assert hasattr(ray_iter, "__iter__")
        assert hasattr(ray_iter, "__next__")
        assert hasattr(tpe_iter, "__iter__")
        assert hasattr(tpe_iter, "__next__")
        assert type(ray_result) == type(tpe_result)
        assert sorted(ray_result) == sorted(tpe_result)

    def test_conformity_with_processpool_using_max_workers(self):
        def f_process0(x):
            return len([i for i in range(x) if i % 2 == 0])

        assert f_process0.__code__.co_code == f_process1.__code__.co_code

        with RayExecutor(max_workers=2) as ex:
            ray_result = ex.submit(f_process0, 100).result()
        with ProcessPoolExecutor(max_workers=2) as ppe:
            ppe_result = ppe.submit(f_process1, 100).result()
        assert type(ray_result) == type(ppe_result)
        assert ray_result == ppe_result

    def test_conformity_with_processpool_map_using_max_workers(self):
        def f_process0(x):
            return len([i for i in range(x) if i % 2 == 0])

        assert f_process0.__code__.co_code == f_process1.__code__.co_code

        with RayExecutor(max_workers=2) as ex:
            ray_iter = ex.map(f_process0, range(10))
            ray_result = list(ray_iter)
        with ProcessPoolExecutor(max_workers=2) as ppe:
            ppe_iter = ppe.map(f_process1, range(10))
            ppe_result = list(ppe_iter)
        assert hasattr(ray_iter, "__iter__")
        assert hasattr(ray_iter, "__next__")
        assert hasattr(ppe_iter, "__iter__")
        assert hasattr(ppe_iter, "__next__")
        assert type(ray_result) == type(ppe_result)
        assert sorted(ray_result) == sorted(ppe_result)

    def test_conformity_with_threadpool_using_max_workers(self):
        def f_process0(x):
            return len([i for i in range(x) if i % 2 == 0])

        assert f_process0.__code__.co_code == f_process1.__code__.co_code

        with RayExecutor(max_workers=2) as ex:
            ray_future = ex.submit(f_process0, 100)
            ray_future_type = type(ray_future)
            ray_result = ray_future.result()
        with ThreadPoolExecutor(max_workers=2) as tpe:
            tpe_future = tpe.submit(f_process1, 100)
            tpe_future_type = type(tpe_future)
            tpe_result = tpe_future.result()
        assert ray_future_type == tpe_future_type
        assert ray_result == tpe_result

    def test_conformity_with_threadpool_map_using_max_workers(self):
        def f_process0(x):
            return len([i for i in range(x) if i % 2 == 0])

        assert f_process0.__code__.co_code == f_process1.__code__.co_code

        with RayExecutor(max_workers=2) as ex:
            ray_iter = ex.map(f_process0, range(10))
            ray_result = list(ray_iter)
        with ThreadPoolExecutor(max_workers=2) as tpe:
            tpe_iter = tpe.map(f_process1, range(10))
            tpe_result = list(tpe_iter)
        assert hasattr(ray_iter, "__iter__")
        assert hasattr(ray_iter, "__next__")
        assert hasattr(tpe_iter, "__iter__")
        assert hasattr(tpe_iter, "__next__")
        assert type(ray_result) == type(tpe_result)
        assert sorted(ray_result) == sorted(tpe_result)

    def test_conformity_with_threadpool_initializer_initargs(self):

        assert os.path.isdir("./python/ray/tests/.")

        #----------------------------
        with ThreadPoolExecutor(max_workers=2, initializer=safe, initargs=(TestInitializerException,)) as tpe:
            tpe_iter = tpe.map(f_process1, range(10))
            _ = list(tpe_iter)
        with ThreadPoolExecutor(max_workers=2, initializer=unsafe, initargs=(TestInitializerException,)) as tpe:
            tpe_iter = tpe.map(f_process1, range(10))
            with pytest.raises(BrokenThreadPool):
                _ = list(tpe_iter)
        #----------------------------

        #----------------------------
        with RayExecutor(max_workers=2, initializer=safe, initargs=(TestInitializerException,), runtime_env={"working_dir": "./python/ray/tests/."}) as ex:
            ray_iter = ex.map(lambda x: x, range(10))
            _ = list(ray_iter)
        with RayExecutor(max_workers=2, initializer=unsafe, initargs=(TestInitializerException,), runtime_env={"working_dir": "./python/ray/tests/."}) as ex:
            ray_iter = ex.map(f_process1, range(10))
            with pytest.raises(RayTaskError):
                _ = list(ray_iter)
        #----------------------------

    def test_working_directory_must_be_supplied_for_initializer(self):

        with pytest.raises(ValueError):
            with RayExecutor(max_workers=2, initializer=safe, initargs=(TestInitializerException,)) as _:
                pass
        with RayExecutor(max_workers=2, initializer=unsafe, initargs=(TestInitializerException,), runtime_env={"working_dir": "./python/ray/tests/."}) as _:
            pass

    def test_mp_context_does_nothing(self):
        with RayExecutor(max_workers=2, mp_context="fork") as ex:
            assert ex._mp_context == "fork"


if __name__ == "__main__":
    if os.environ.get("PARALLEL_CI"):
        sys.exit(pytest.main(["-n", "auto", "--boxed", "-vs", __file__]))
    else:
        sys.exit(pytest.main(["-sv", __file__]))
