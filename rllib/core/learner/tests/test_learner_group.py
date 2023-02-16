import gymnasium as gym
import itertools
import numpy as np
import unittest

import ray
from ray.rllib.policy.sample_batch import DEFAULT_POLICY_ID, MultiAgentBatch
from ray.rllib.utils.test_utils import check, get_cartpole_dataset_reader
from ray.rllib.core.learner.scaling_config import LearnerGroupScalingConfig
from ray.rllib.core.testing.utils import (
    get_learner_group,
    get_learner,
    add_module_to_learner_or_learner_group,
)
from ray.util.timer import _Timer


REMOTE_SCALING_CONFIGS = {
    "remote-cpu": LearnerGroupScalingConfig(num_workers=1),
    "remote-gpu": LearnerGroupScalingConfig(num_workers=1, num_gpus_per_worker=0.5),
    "multi-gpu-ddp": LearnerGroupScalingConfig(num_workers=2, num_gpus_per_worker=1),
    "multi-cpu-ddp": LearnerGroupScalingConfig(num_workers=2, num_cpus_per_worker=2),
    # "multi-gpu-ddp-pipeline": LearnerGroupScalingConfig(
    #     num_workers=2, num_gpus_per_worker=2
    # ),
}


LOCAL_SCALING_CONFIGS = {
    "local-cpu": LearnerGroupScalingConfig(num_workers=0, num_gpus_per_worker=0),
    "local-gpu": LearnerGroupScalingConfig(num_workers=0, num_gpus_per_worker=0.5),
}


# TODO(avnishn) Make this a ray task later. Currently thats not possible because the
# task is not dying after the test is done. This is a bug with ray core.
@ray.remote(num_gpus=1)
class RemoteTrainingHelper:
    def local_training_helper(self, fw, scaling_mode) -> None:
        env = gym.make("CartPole-v1")
        scaling_config = LOCAL_SCALING_CONFIGS[scaling_mode]
        learner_group = get_learner_group(fw, env, scaling_config, eager_tracing=True)
        local_learner = get_learner(fw, env)
        local_learner.build()

        # make the state of the learner and the local learner_group identical
        local_learner.set_state(learner_group.get_state())

        reader = get_cartpole_dataset_reader(batch_size=500)
        batch = reader.next()
        batch = batch.as_multi_agent()
        check(local_learner.update(batch), learner_group.update(batch))

        new_module_id = "test_module"

        add_module_to_learner_or_learner_group(fw, env, new_module_id, learner_group)
        add_module_to_learner_or_learner_group(fw, env, new_module_id, local_learner)

        # make the state of the learner and the local learner_group identical
        local_learner.set_state(learner_group.get_state())

        # do another update
        batch = reader.next()
        ma_batch = MultiAgentBatch(
            {new_module_id: batch, DEFAULT_POLICY_ID: batch}, env_steps=batch.count
        )
        check(local_learner.update(ma_batch), learner_group.update(ma_batch))

        check(local_learner.get_state(), learner_group.get_state())


class TestLearnerGroup(unittest.TestCase):
    def setUp(self) -> None:
        ray.init()

    def tearDown(self) -> None:
        ray.shutdown()

    def test_learner_group_local(self):
        fws = ["tf", "torch"]

        test_iterator = itertools.product(fws, LOCAL_SCALING_CONFIGS)

        # run the logic of this test inside of a ray actor because we want tensorflow
        # resources to be gracefully released. Tensorflow blocks the gpu resources
        # otherwise between test cases, causing a gpu oom error.
        for fw, scaling_mode in test_iterator:
            print(f"Testing framework: {fw}, scaling mode: {scaling_mode}")
            training_helper = RemoteTrainingHelper.remote()
            ray.get(training_helper.local_training_helper.remote(fw, scaling_mode))

    def test_update_multigpu(self):
        fws = ["tf", "torch"]
        scaling_modes = REMOTE_SCALING_CONFIGS.keys()
        test_iterator = itertools.product(fws, scaling_modes)

        for fw, scaling_mode in test_iterator:
            print(f"Testing framework: {fw}, scaling mode: {scaling_mode}.")
            env = gym.make("CartPole-v1")

            scaling_config = REMOTE_SCALING_CONFIGS[scaling_mode]
            learner_group = get_learner_group(
                fw, env, scaling_config, eager_tracing=True
            )
            reader = get_cartpole_dataset_reader(batch_size=1024)

            min_loss = float("inf")
            for iter_i in range(1000):
                batch = reader.next()
                results = learner_group.update(batch.as_multi_agent(), reduce_fn=None)

                loss = np.mean([res["loss"]["total_loss"] for res in results])
                min_loss = min(loss, min_loss)
                print(f"[iter = {iter_i}] Loss: {loss:.3f}, Min Loss: {min_loss:.3f}")
                # The loss is initially around 0.69 (ln2). When it gets to around
                # 0.57 the return of the policy gets to around 100.
                if min_loss < 0.57:
                    break

                for res1, res2 in zip(results, results[1:]):
                    self.assertEqual(
                        res1["mean_weight"]["default_policy"],
                        res2["mean_weight"]["default_policy"],
                    )

            self.assertLess(min_loss, 0.57)

            # make sure the learner_group resources are freed up so that we don't
            # autoscale
            learner_group.shutdown()
            del learner_group

    def test_add_remove_module(self):
        fws = ["tf", "torch"]
        scaling_modes = REMOTE_SCALING_CONFIGS.keys()
        test_iterator = itertools.product(fws, scaling_modes)

        for fw, scaling_mode in test_iterator:
            print(f"Testing framework: {fw}, scaling mode: {scaling_mode}.")
            env = gym.make("CartPole-v1")
            scaling_config = REMOTE_SCALING_CONFIGS[scaling_mode]
            learner_group = get_learner_group(
                fw, env, scaling_config, eager_tracing=True
            )
            reader = get_cartpole_dataset_reader(batch_size=512)
            batch = reader.next()

            # update once with the default policy
            results = learner_group.update(batch.as_multi_agent(), reduce_fn=None)
            module_ids_before_add = {DEFAULT_POLICY_ID}
            new_module_id = "test_module"

            # add a test_module
            add_module_to_learner_or_learner_group(
                fw, env, new_module_id, learner_group
            )

            # do training that includes the test_module
            results = learner_group.update(
                MultiAgentBatch(
                    {new_module_id: batch, DEFAULT_POLICY_ID: batch}, batch.count
                ),
                reduce_fn=None,
            )

            # check that module weights are updated across workers and synchronized
            for i in range(1, len(results)):
                for module_id in results[i]["mean_weight"].keys():
                    assert (
                        results[i]["mean_weight"][module_id]
                        == results[i - 1]["mean_weight"][module_id]
                    )

            # check that module ids are updated to include the new module
            module_ids_after_add = {DEFAULT_POLICY_ID, new_module_id}
            for result in results:
                # remove the total_loss key since its not a module key
                self.assertEqual(
                    set(result["loss"]) - {"total_loss"}, module_ids_after_add
                )

            # remove the test_module
            learner_group.remove_module(module_id=new_module_id)

            # run training without the test_module
            results = learner_group.update(batch.as_multi_agent(), reduce_fn=None)

            # check that module weights are updated across workers and synchronized
            for i in range(1, len(results)):
                for module_id in results[i]["mean_weight"].keys():
                    assert (
                        results[i]["mean_weight"][module_id]
                        == results[i - 1]["mean_weight"][module_id]
                    )

            # check that module ids are updated after remove operation to not
            # include the new module
            for result in results:
                # remove the total_loss key since its not a module key
                self.assertEqual(
                    set(result["loss"]) - {"total_loss"}, module_ids_before_add
                )

            # make sure the learner_group resources are freed up so that we don't
            # autoscale
            learner_group.shutdown()
            del learner_group

    def test_async_update(self):
        """Test that async style updates converge to the same result as sync."""
        fws = ["tf", "torch"]
        # block=True only needs to be tested for the most complex case.
        # so we'll only test it for multi-gpu-ddp.
        scaling_modes = ["multi-gpu-ddp"]
        test_iterator = itertools.product(fws, scaling_modes)

        for fw, scaling_mode in test_iterator:
            print(f"Testing framework: {fw}, scaling mode: {scaling_mode}.")
            env = gym.make("CartPole-v1")
            scaling_config = REMOTE_SCALING_CONFIGS[scaling_mode]
            learner_group = get_learner_group(fw, env, scaling_config)
            reader = get_cartpole_dataset_reader(batch_size=512)
            min_loss = float("inf")
            batch = reader.next()
            timer_sync = _Timer()
            timer_async = _Timer()
            with timer_sync:
                learner_group.update(batch.as_multi_agent(), block=True, reduce_fn=None)
            with timer_async:
                result_async = learner_group.update(
                    batch.as_multi_agent(), block=False, reduce_fn=None
                )
            # ideally the the first async update will return nothing, and an easy
            # way to check that is if the time for an async update call is faster
            # than the time for a sync update call.
            self.assertLess(timer_async.mean, timer_sync.mean)
            self.assertIsInstance(result_async, list)
            self.assertEqual(len(result_async), 0)
            for iter_i in range(1000):
                batch = reader.next()
                results = learner_group.update(
                    batch.as_multi_agent(), block=False, reduce_fn=None
                )
                if not results:
                    continue
                loss = np.mean([res["loss"]["total_loss"] for res in results])
                min_loss = min(loss, min_loss)
                print(f"[iter = {iter_i}] Loss: {loss:.3f}, Min Loss: {min_loss:.3f}")
                # The loss is initially around 0.69 (ln2). When it gets to around
                # 0.57 the return of the policy gets to around 100.
                if min_loss < 0.57:
                    break

                for res1, res2 in zip(results, results[1:]):
                    self.assertEqual(
                        res1["mean_weight"]["default_policy"],
                        res2["mean_weight"]["default_policy"],
                    )
            learner_group.shutdown()
            self.assertLess(min_loss, 0.57)


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main(["-v", __file__]))