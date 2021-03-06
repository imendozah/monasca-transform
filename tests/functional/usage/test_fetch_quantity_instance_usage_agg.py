# Copyright 2016 Hewlett Packard Enterprise Development Company LP
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import json
import unittest

import mock
from oslo_config import cfg
from pyspark.sql import SQLContext
from pyspark.streaming.kafka import OffsetRange
from tests.functional.component.insert.dummy_insert import DummyInsert
from tests.functional.spark_context_test import SparkContextTest
from tests.functional.test_resources.fetch_quantity_data.data_provider \
    import DataProvider
from tests.functional.test_resources.fetch_quantity_data_second_stage.data_provider \
    import DataProvider as SecondStageDataProvider
from tests.functional.test_resources.mock_component_manager \
    import MockComponentManager
from tests.functional.test_resources.mock_data_driven_specs_repo \
    import MockDataDrivenSpecsRepo
from tests.functional.usage import dump_as_ascii_string

from monasca_transform.config.config_initializer import ConfigInitializer
from monasca_transform.driver.mon_metrics_kafka \
    import MonMetricsKafkaProcessor
from monasca_transform.processor.pre_hourly_processor import PreHourlyProcessor
from monasca_transform.transform import RddTransformContext
from monasca_transform.transform import TransformContextUtils
from tests.functional.messaging.adapter import DummyAdapter


class TestFetchQuantityInstanceUsageAgg(SparkContextTest):

    def setUp(self):
        super(TestFetchQuantityInstanceUsageAgg, self).setUp()
        # configure the system with a dummy messaging adapter
        ConfigInitializer.basic_config(
            default_config_files=[
                'tests/functional/test_resources/config/'
                'test_config_with_dummy_messaging_adapter.conf'])
        # reset metric_id list dummy adapter
        if not DummyAdapter.adapter_impl:
            DummyAdapter.init()
        DummyAdapter.adapter_impl.metric_list = []

    def get_pre_transform_specs_json(self):
        """get pre_transform_specs driver table info."""
        pre_transform_specs_json = """
        {"event_processing_params":{"set_default_zone_to":"1",
                                    "set_default_geolocation_to":"1",
                                    "set_default_region_to":"W"},
         "event_type":"mem.total_mb",
         "metric_id_list":["mem_total_all"],
         "required_raw_fields_list":["creation_time"]}"""
        return [json.loads(pre_transform_specs_json)]

    def get_transform_specs_json_by_operation(self,
                                              usage_fetch_operation):
        """get transform_specs driver table info."""
        transform_specs_json = """
        {"aggregation_params_map":{
               "aggregation_pipeline":{"source":"streaming",
                                       "usage":"fetch_quantity",
                                       "setters":["rollup_quantity",
                                                  "set_aggregated_metric_name",
                                                  "set_aggregated_period"],
                                       "insert":["prepare_data",
                                                 "insert_data_pre_hourly"]},
               "aggregated_metric_name": "mem.total_mb_agg",
               "aggregation_period": "hourly",
               "aggregation_group_by_list": ["host", "metric_id"],
               "usage_fetch_operation": "%s",
               "setter_rollup_group_by_list": ["host"],
               "setter_rollup_operation": "sum",
               "pre_hourly_operation":"%s",
               "pre_hourly_group_by_list":["default"],
               "dimension_list":["aggregation_period",
                                 "host",
                                 "project_id"]
         },
         "metric_group":"mem_total_all",
         "metric_id":"mem_total_all"}"""
        transform_specs_json_operation = \
            transform_specs_json % (usage_fetch_operation,
                                    usage_fetch_operation)
        return [json.loads(transform_specs_json_operation)]

    @mock.patch('monasca_transform.processor.pre_hourly_processor.KafkaInsert',
                DummyInsert)
    @mock.patch('monasca_transform.data_driven_specs.data_driven_specs_repo.'
                'DataDrivenSpecsRepoFactory.get_data_driven_specs_repo')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_insert_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_setter_component_manager')
    @mock.patch('monasca_transform.transform.'
                'builder.generic_transform_builder.GenericTransformBuilder.'
                '_get_usage_component_manager')
    def test_fetch_quantity_max(self,
                                usage_manager,
                                setter_manager,
                                insert_manager,
                                data_driven_specs_repo):

        # test operation
        test_operation = "max"

        # load components
        usage_manager.return_value = MockComponentManager.get_usage_cmpt_mgr()
        setter_manager.return_value = \
            MockComponentManager.get_setter_cmpt_mgr()
        insert_manager.return_value = \
            MockComponentManager.get_insert_pre_hourly_cmpt_mgr()

        # init mock driver tables
        data_driven_specs_repo.return_value = \
            MockDataDrivenSpecsRepo(self.spark_context,
                                    self.get_pre_transform_specs_json(),
                                    self.get_transform_specs_json_by_operation(
                                        test_operation))

        # Create an emulated set of Kafka messages (these were gathered
        # by extracting Monasca messages from the Metrics queue on mini-mon).

        # Create an RDD out of the mocked Monasca metrics
        with open(DataProvider.fetch_quantity_data_path) as f:
            raw_lines = f.read().splitlines()
        raw_tuple_list = [eval(raw_line) for raw_line in raw_lines]

        rdd_monasca = self.spark_context.parallelize(raw_tuple_list)

        # decorate mocked RDD with dummy kafka offsets
        myOffsetRanges = [
            OffsetRange("metrics", 1, 10, 20)]  # mimic rdd.offsetRanges()

        transform_context = TransformContextUtils.get_context(
            offset_info=myOffsetRanges,
            batch_time_info=self.get_dummy_batch_time())
        rdd_monasca_with_offsets = rdd_monasca.map(
            lambda x: RddTransformContext(x, transform_context))

        # Call the primary method in mon_metrics_kafka
        MonMetricsKafkaProcessor.rdd_to_recordstore(
            rdd_monasca_with_offsets)

        # get the metrics that have been submitted to the dummy message adapter
        instance_usage_list = DummyAdapter.adapter_impl.metric_list
        instance_usage_list = map(dump_as_ascii_string,
                                  instance_usage_list)
        DummyAdapter.adapter_impl.metric_list = []
        instance_usage_rdd = self.spark_context.parallelize(
            instance_usage_list)
        sql_context = SQLContext(self.spark_context)
        instance_usage_df = sql_context.read.json(instance_usage_rdd)
        PreHourlyProcessor.do_transform(instance_usage_df)

        metrics = DummyAdapter.adapter_impl.metric_list
        mem_total_mb_agg_metric = [
            value for value in metrics
            if value.get('metric').get('name') ==
            'mem.total_mb_agg' and
            value.get('metric').get('dimensions').get('host') ==
            'mini-mon'][0]

        self.assertTrue(mem_total_mb_agg_metric is not None)

        self.assertEqual('mem.total_mb_agg',
                         mem_total_mb_agg_metric
                         .get('metric').get('name'))

        self.assertEqual(8192.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value'))
        self.assertEqual('useast',
                         mem_total_mb_agg_metric
                         .get('meta').get('region'))

        self.assertEqual(cfg.CONF.messaging.publish_kafka_project_id,
                         mem_total_mb_agg_metric
                         .get('meta').get('tenantId'))
        self.assertEqual('mini-mon',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('host'))
        self.assertEqual('all',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('project_id'))
        self.assertEqual('hourly',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions')
                         .get('aggregation_period'))
        self.assertEqual(4.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta').get('record_count'))
        self.assertEqual('2016-01-20 16:40:00',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('firstrecord_timestamp_string'))
        self.assertEqual('2016-01-20 16:40:46',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('lastrecord_timestamp_string'))

    @mock.patch('monasca_transform.processor.pre_hourly_processor.KafkaInsert',
                DummyInsert)
    @mock.patch('monasca_transform.data_driven_specs.data_driven_specs_repo.'
                'DataDrivenSpecsRepoFactory.get_data_driven_specs_repo')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_insert_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_setter_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_usage_component_manager')
    def test_fetch_quantity_min(self,
                                usage_manager,
                                setter_manager,
                                insert_manager,
                                data_driven_specs_repo):

        # test operation
        test_operation = "min"

        # load components
        usage_manager.return_value = MockComponentManager.get_usage_cmpt_mgr()
        setter_manager.return_value = \
            MockComponentManager.get_setter_cmpt_mgr()
        insert_manager.return_value = \
            MockComponentManager.get_insert_pre_hourly_cmpt_mgr()

        # init mock driver tables
        data_driven_specs_repo.return_value = \
            MockDataDrivenSpecsRepo(self.spark_context,
                                    self.get_pre_transform_specs_json(),
                                    self.get_transform_specs_json_by_operation(
                                        test_operation))

        # Create an emulated set of Kafka messages (these were gathered
        # by extracting Monasca messages from the Metrics queue on mini-mon).

        # Create an RDD out of the mocked Monasca metrics
        with open(DataProvider.fetch_quantity_data_path) as f:
            raw_lines = f.read().splitlines()
        raw_tuple_list = [eval(raw_line) for raw_line in raw_lines]

        rdd_monasca = self.spark_context.parallelize(raw_tuple_list)

        # decorate mocked RDD with dummy kafka offsets
        myOffsetRanges = [
            OffsetRange("metrics", 1, 10, 20)]  # mimic rdd.offsetRanges()

        transform_context = TransformContextUtils.get_context(
            offset_info=myOffsetRanges,
            batch_time_info=self.get_dummy_batch_time())

        rdd_monasca_with_offsets = rdd_monasca.map(
            lambda x: RddTransformContext(x, transform_context))

        # Call the primary method in mon_metrics_kafka
        MonMetricsKafkaProcessor.rdd_to_recordstore(
            rdd_monasca_with_offsets)

        # get the metrics that have been submitted to the dummy message adapter
        instance_usage_list = DummyAdapter.adapter_impl.metric_list
        instance_usage_list = map(dump_as_ascii_string,
                                  instance_usage_list)
        DummyAdapter.adapter_impl.metric_list = []
        instance_usage_rdd = self.spark_context.parallelize(
            instance_usage_list)
        sql_context = SQLContext(self.spark_context)
        instance_usage_df = sql_context.read.json(instance_usage_rdd)
        PreHourlyProcessor.do_transform(instance_usage_df)

        metrics = DummyAdapter.adapter_impl.metric_list
        mem_total_mb_agg_metric = [
            value for value in metrics
            if value.get('metric').get('name') ==
            'mem.total_mb_agg' and
            value.get('metric').get('dimensions').get('host') ==
            'mini-mon'][0]

        self.assertTrue(mem_total_mb_agg_metric is not None)

        self.assertEqual('mem.total_mb_agg',
                         mem_total_mb_agg_metric
                         .get('metric').get('name'))

        self.assertEqual(1024.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value'))
        self.assertEqual('useast',
                         mem_total_mb_agg_metric
                         .get('meta').get('region'))

        self.assertEqual(cfg.CONF.messaging.publish_kafka_project_id,
                         mem_total_mb_agg_metric
                         .get('meta').get('tenantId'))
        self.assertEqual('mini-mon',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('host'))
        self.assertEqual('all',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('project_id'))
        self.assertEqual('hourly',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions')
                         .get('aggregation_period'))
        self.assertEqual(4.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta').get('record_count'))
        self.assertEqual('2016-01-20 16:40:00',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('firstrecord_timestamp_string'))
        self.assertEqual('2016-01-20 16:40:46',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('lastrecord_timestamp_string'))

    @mock.patch('monasca_transform.processor.pre_hourly_processor.KafkaInsert',
                DummyInsert)
    @mock.patch('monasca_transform.data_driven_specs.data_driven_specs_repo.'
                'DataDrivenSpecsRepoFactory.get_data_driven_specs_repo')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_insert_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_setter_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_usage_component_manager')
    def test_fetch_quantity_avg(self,
                                usage_manager,
                                setter_manager,
                                insert_manager,
                                data_driven_specs_repo):

        # test operation
        test_operation = "avg"

        # load components
        usage_manager.return_value = MockComponentManager.get_usage_cmpt_mgr()
        setter_manager.return_value = \
            MockComponentManager.get_setter_cmpt_mgr()
        insert_manager.return_value = \
            MockComponentManager.get_insert_pre_hourly_cmpt_mgr()

        # init mock driver tables
        data_driven_specs_repo.return_value = \
            MockDataDrivenSpecsRepo(self.spark_context,
                                    self.get_pre_transform_specs_json(),
                                    self.get_transform_specs_json_by_operation(
                                        test_operation))

        # Create an emulated set of Kafka messages (these were gathered
        # by extracting Monasca messages from the Metrics queue on mini-mon).

        # Create an RDD out of the mocked Monasca metrics
        with open(DataProvider.fetch_quantity_data_path) as f:
            raw_lines = f.read().splitlines()
        raw_tuple_list = [eval(raw_line) for raw_line in raw_lines]

        rdd_monasca = self.spark_context.parallelize(raw_tuple_list)

        # decorate mocked RDD with dummy kafka offsets
        myOffsetRanges = [
            OffsetRange("metrics", 1, 10, 20)]  # mimic rdd.offsetRanges()

        transform_context = TransformContextUtils.get_context(
            offset_info=myOffsetRanges,
            batch_time_info=self.get_dummy_batch_time())

        rdd_monasca_with_offsets = rdd_monasca.map(
            lambda x: RddTransformContext(x, transform_context))

        # Call the primary method in mon_metrics_kafka
        MonMetricsKafkaProcessor.rdd_to_recordstore(
            rdd_monasca_with_offsets)

        # get the metrics that have been submitted to the dummy message adapter
        instance_usage_list = DummyAdapter.adapter_impl.metric_list
        instance_usage_list = map(dump_as_ascii_string,
                                  instance_usage_list)
        DummyAdapter.adapter_impl.metric_list = []
        instance_usage_rdd = self.spark_context.parallelize(
            instance_usage_list)
        sql_context = SQLContext(self.spark_context)
        instance_usage_df = sql_context.read.json(instance_usage_rdd)
        PreHourlyProcessor.do_transform(instance_usage_df)

        metrics = DummyAdapter.adapter_impl.metric_list
        mem_total_mb_agg_metric = [
            value for value in metrics
            if value.get('metric').get('name') ==
            'mem.total_mb_agg' and
            value.get('metric').get('dimensions').get('host') ==
            'mini-mon'][0]

        self.assertTrue(mem_total_mb_agg_metric is not None)

        self.assertEqual('mem.total_mb_agg',
                         mem_total_mb_agg_metric
                         .get('metric').get('name'))

        self.assertEqual(3840.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value'))
        self.assertEqual('useast',
                         mem_total_mb_agg_metric
                         .get('meta').get('region'))

        self.assertEqual(cfg.CONF.messaging.publish_kafka_project_id,
                         mem_total_mb_agg_metric
                         .get('meta').get('tenantId'))
        self.assertEqual('mini-mon',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('host'))
        self.assertEqual('all',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('project_id'))
        self.assertEqual('hourly',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions')
                         .get('aggregation_period'))
        self.assertEqual(4.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta').get('record_count'))
        self.assertEqual('2016-01-20 16:40:00',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('firstrecord_timestamp_string'))
        self.assertEqual('2016-01-20 16:40:46',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('lastrecord_timestamp_string'))

    @mock.patch('monasca_transform.processor.pre_hourly_processor.KafkaInsert',
                DummyInsert)
    @mock.patch('monasca_transform.data_driven_specs.data_driven_specs_repo.'
                'DataDrivenSpecsRepoFactory.get_data_driven_specs_repo')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_insert_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_setter_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_usage_component_manager')
    def test_fetch_quantity_sum(self,
                                usage_manager,
                                setter_manager,
                                insert_manager,
                                data_driven_specs_repo):

        # test operation
        test_operation = "sum"

        # load components
        usage_manager.return_value = MockComponentManager.get_usage_cmpt_mgr()
        setter_manager.return_value = \
            MockComponentManager.get_setter_cmpt_mgr()
        insert_manager.return_value = \
            MockComponentManager.get_insert_pre_hourly_cmpt_mgr()

        # init mock driver tables
        data_driven_specs_repo.return_value = \
            MockDataDrivenSpecsRepo(self.spark_context,
                                    self.get_pre_transform_specs_json(),
                                    self.get_transform_specs_json_by_operation(
                                        test_operation))

        # Create an emulated set of Kafka messages (these were gathered
        # by extracting Monasca messages from the Metrics queue on mini-mon).

        # Create an RDD out of the mocked Monasca metrics
        with open(DataProvider.fetch_quantity_data_path) as f:
            raw_lines = f.read().splitlines()
        raw_tuple_list = [eval(raw_line) for raw_line in raw_lines]

        rdd_monasca = self.spark_context.parallelize(raw_tuple_list)

        # decorate mocked RDD with dummy kafka offsets
        myOffsetRanges = [
            OffsetRange("metrics", 1, 10, 20)]  # mimic rdd.offsetRanges()

        transform_context = TransformContextUtils.get_context(
            offset_info=myOffsetRanges,
            batch_time_info=self.get_dummy_batch_time())

        rdd_monasca_with_offsets = rdd_monasca.map(
            lambda x: RddTransformContext(x, transform_context))

        # Call the primary method in mon_metrics_kafka
        MonMetricsKafkaProcessor.rdd_to_recordstore(
            rdd_monasca_with_offsets)

        # get the metrics that have been submitted to the dummy message adapter
        instance_usage_list = DummyAdapter.adapter_impl.metric_list
        instance_usage_list = map(dump_as_ascii_string,
                                  instance_usage_list)
        DummyAdapter.adapter_impl.metric_list = []
        instance_usage_rdd = self.spark_context.parallelize(
            instance_usage_list)
        sql_context = SQLContext(self.spark_context)
        instance_usage_df = sql_context.read.json(instance_usage_rdd)
        PreHourlyProcessor.do_transform(instance_usage_df)

        metrics = DummyAdapter.adapter_impl.metric_list
        mem_total_mb_agg_metric = [
            value for value in metrics
            if value.get('metric').get('name') ==
            'mem.total_mb_agg' and
            value.get('metric').get('dimensions').get('host') ==
            'mini-mon'][0]

        self.assertTrue(mem_total_mb_agg_metric is not None)

        self.assertEqual('mem.total_mb_agg',
                         mem_total_mb_agg_metric
                         .get('metric').get('name'))

        self.assertEqual(15360.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value'))
        self.assertEqual('useast',
                         mem_total_mb_agg_metric
                         .get('meta').get('region'))

        self.assertEqual(cfg.CONF.messaging.publish_kafka_project_id,
                         mem_total_mb_agg_metric
                         .get('meta').get('tenantId'))
        self.assertEqual('mini-mon',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('host'))
        self.assertEqual('all',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('project_id'))
        self.assertEqual('hourly',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions')
                         .get('aggregation_period'))
        self.assertEqual(4.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta').get('record_count'))
        self.assertEqual('2016-01-20 16:40:00',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('firstrecord_timestamp_string'))
        self.assertEqual('2016-01-20 16:40:46',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('lastrecord_timestamp_string'))

    @mock.patch('monasca_transform.processor.pre_hourly_processor.KafkaInsert',
                DummyInsert)
    @mock.patch('monasca_transform.data_driven_specs.data_driven_specs_repo.'
                'DataDrivenSpecsRepoFactory.get_data_driven_specs_repo')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_insert_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_setter_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_usage_component_manager')
    def test_fetch_quantity_max_second_stage(self,
                                             usage_manager,
                                             setter_manager,
                                             insert_manager,
                                             data_driven_specs_repo):

        # test operation
        test_operation = "max"

        # load components
        usage_manager.return_value = MockComponentManager.get_usage_cmpt_mgr()
        setter_manager.return_value = \
            MockComponentManager.get_setter_cmpt_mgr()
        insert_manager.return_value = \
            MockComponentManager.get_insert_pre_hourly_cmpt_mgr()

        # init mock driver tables
        data_driven_specs_repo.return_value = \
            MockDataDrivenSpecsRepo(self.spark_context,
                                    self.get_pre_transform_specs_json(),
                                    self.get_transform_specs_json_by_operation(
                                        test_operation))

        # Create an emulated set of Kafka messages (these were gathered
        # by extracting Monasca messages from the Metrics queue on mini-mon).

        # Create an RDD out of the mocked Monasca metrics
        with open(SecondStageDataProvider.fetch_quantity_data_path) as f:
            raw_lines = f.read().splitlines()
        raw_tuple_list = [eval(raw_line) for raw_line in raw_lines]
        instance_usage_list = map(dump_as_ascii_string,
                                  raw_tuple_list)

        # get the instance usage that have been
        # submitted to the dummy message adapter
        # create a json RDD from instance_usage_list
        instance_usage_rdd = self.spark_context.parallelize(
            instance_usage_list)

        sql_context = SQLContext(self.spark_context)
        instance_usage_df = sql_context.read.json(
            instance_usage_rdd)

        # call pre hourly processor
        PreHourlyProcessor.do_transform(instance_usage_df)
        metrics = DummyAdapter.adapter_impl.metric_list
        mem_total_mb_agg_metric = [
            value for value in metrics
            if value.get('metric').get('name') ==
            'mem.total_mb_agg' and
            value.get('metric').get('dimensions').get('host') ==
            'mini-mon'][0]
        self.assertTrue(mem_total_mb_agg_metric is not None)
        self.assertEqual('mem.total_mb_agg',
                         mem_total_mb_agg_metric
                         .get('metric').get('name'))

        self.assertEqual(2318.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value'))
        self.assertEqual('useast',
                         mem_total_mb_agg_metric
                         .get('meta').get('region'))

        self.assertEqual(cfg.CONF.messaging.publish_kafka_project_id,
                         mem_total_mb_agg_metric
                         .get('meta').get('tenantId'))
        self.assertEqual('mini-mon',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('host'))
        self.assertEqual('all',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('project_id'))
        self.assertEqual('prehourly',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions')
                         .get('aggregation_period'))

        self.assertEqual(39.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta').get('record_count'))
        self.assertEqual('2016-01-20 16:40:00',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('firstrecord_timestamp_string'))
        self.assertEqual('2016-01-20 17:40:00',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('lastrecord_timestamp_string'))

    @mock.patch('monasca_transform.processor.pre_hourly_processor.KafkaInsert',
                DummyInsert)
    @mock.patch('monasca_transform.data_driven_specs.data_driven_specs_repo.'
                'DataDrivenSpecsRepoFactory.get_data_driven_specs_repo')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_insert_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_setter_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_usage_component_manager')
    def test_fetch_quantity_min_second_stage(self,
                                             usage_manager,
                                             setter_manager,
                                             insert_manager,
                                             data_driven_specs_repo):

        # test operation
        test_operation = "min"
        # load components
        usage_manager.return_value = MockComponentManager.get_usage_cmpt_mgr()
        setter_manager.return_value = \
            MockComponentManager.get_setter_cmpt_mgr()
        insert_manager.return_value = \
            MockComponentManager.get_insert_pre_hourly_cmpt_mgr()

        # init mock driver tables
        data_driven_specs_repo.return_value = \
            MockDataDrivenSpecsRepo(self.spark_context,
                                    self.get_pre_transform_specs_json(),
                                    self.get_transform_specs_json_by_operation(
                                        test_operation))

        # Create an emulated set of Kafka messages (these were gathered
        # by extracting Monasca messages from the Metrics queue on mini-mon).

        # Create an RDD out of the mocked Monasca metrics
        with open(SecondStageDataProvider.fetch_quantity_data_path) as f:
            raw_lines = f.read().splitlines()
        raw_tuple_list = [eval(raw_line) for raw_line in raw_lines]
        instance_usage_list = map(dump_as_ascii_string,
                                  raw_tuple_list)

        # create a json RDD from instance_usage_list
        instance_usage_rdd = self.spark_context.parallelize(
            instance_usage_list)

        sql_context = SQLContext(self.spark_context)
        instance_usage_df = sql_context.read.json(
            instance_usage_rdd)

        PreHourlyProcessor.do_transform(instance_usage_df)

        metrics = DummyAdapter.adapter_impl.metric_list
        mem_total_mb_agg_metric = [
            value for value in metrics
            if value.get('metric').get('name') ==
            'mem.total_mb_agg' and
            value.get('metric').get('dimensions').get('host') ==
            'mini-mon'][0]
        self.assertTrue(mem_total_mb_agg_metric is not None)

        self.assertEqual('mem.total_mb_agg',
                         mem_total_mb_agg_metric
                         .get('metric').get('name'))

        self.assertEqual(1213.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value'))
        self.assertEqual('useast',
                         mem_total_mb_agg_metric
                         .get('meta').get('region'))

        self.assertEqual(cfg.CONF.messaging.publish_kafka_project_id,
                         mem_total_mb_agg_metric
                         .get('meta').get('tenantId'))
        self.assertEqual('mini-mon',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('host'))
        self.assertEqual('all',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('project_id'))
        self.assertEqual('prehourly',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions')
                         .get('aggregation_period'))

        self.assertEqual(39.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta').get('record_count'))
        self.assertEqual('2016-01-20 16:40:00',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('firstrecord_timestamp_string'))
        self.assertEqual('2016-01-20 17:40:00',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('lastrecord_timestamp_string'))

    @mock.patch('monasca_transform.processor.pre_hourly_processor.KafkaInsert',
                DummyInsert)
    @mock.patch('monasca_transform.data_driven_specs.data_driven_specs_repo.'
                'DataDrivenSpecsRepoFactory.get_data_driven_specs_repo')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_insert_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_setter_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_usage_component_manager')
    def test_fetch_quantity_avg_second_stage(self,
                                             usage_manager,
                                             setter_manager,
                                             insert_manager,
                                             data_driven_specs_repo):

        # test operation
        test_operation = "avg"
        # load components
        usage_manager.return_value = MockComponentManager.get_usage_cmpt_mgr()
        setter_manager.return_value = \
            MockComponentManager.get_setter_cmpt_mgr()
        insert_manager.return_value = \
            MockComponentManager.get_insert_pre_hourly_cmpt_mgr()

        # init mock driver tables
        data_driven_specs_repo.return_value = \
            MockDataDrivenSpecsRepo(self.spark_context,
                                    self.get_pre_transform_specs_json(),
                                    self.get_transform_specs_json_by_operation(
                                        test_operation))

        # Create an emulated set of Kafka messages (these were gathered
        # by extracting Monasca messages from the Metrics queue on mini-mon).

        # Create an RDD out of the mocked Monasca metrics
        with open(SecondStageDataProvider.fetch_quantity_data_path) as f:
            raw_lines = f.read().splitlines()
        raw_tuple_list = [eval(raw_line) for raw_line in raw_lines]
        instance_usage_list = map(dump_as_ascii_string,
                                  raw_tuple_list)

        # create a json RDD from instance_usage_list
        instance_usage_rdd = self.spark_context.parallelize(
            instance_usage_list)

        sql_context = SQLContext(self.spark_context)
        instance_usage_df = sql_context.read.json(
            instance_usage_rdd)

        # call pre hourly processor
        PreHourlyProcessor.do_transform(instance_usage_df)

        metrics = DummyAdapter.adapter_impl.metric_list
        mem_total_mb_agg_metric = [
            value for value in metrics
            if value.get('metric').get('name') ==
            'mem.total_mb_agg' and
            value.get('metric').get('dimensions').get('host') ==
            'mini-mon'][0]
        self.assertTrue(mem_total_mb_agg_metric is not None)

        self.assertEqual('mem.total_mb_agg',
                         mem_total_mb_agg_metric
                         .get('metric').get('name'))

        self.assertEqual(1446.5,
                         mem_total_mb_agg_metric
                         .get('metric').get('value'))
        self.assertEqual('useast',
                         mem_total_mb_agg_metric
                         .get('meta').get('region'))

        self.assertEqual(cfg.CONF.messaging.publish_kafka_project_id,
                         mem_total_mb_agg_metric
                         .get('meta').get('tenantId'))
        self.assertEqual('mini-mon',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('host'))
        self.assertEqual('all',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('project_id'))
        self.assertEqual('prehourly',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions')
                         .get('aggregation_period'))

        self.assertEqual(39.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta').get('record_count'))
        self.assertEqual('2016-01-20 16:40:00',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('firstrecord_timestamp_string'))
        self.assertEqual('2016-01-20 17:40:00',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('lastrecord_timestamp_string'))

    @mock.patch('monasca_transform.processor.pre_hourly_processor.KafkaInsert',
                DummyInsert)
    @mock.patch('monasca_transform.data_driven_specs.data_driven_specs_repo.'
                'DataDrivenSpecsRepoFactory.get_data_driven_specs_repo')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_insert_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_setter_component_manager')
    @mock.patch('monasca_transform.transform.builder.'
                'generic_transform_builder.GenericTransformBuilder.'
                '_get_usage_component_manager')
    def test_fetch_quantity_sum_second_stage(self,
                                             usage_manager,
                                             setter_manager,
                                             insert_manager,
                                             data_driven_specs_repo):

        # test operation
        test_operation = "sum"
        # load components
        usage_manager.return_value = MockComponentManager.get_usage_cmpt_mgr()
        setter_manager.return_value = \
            MockComponentManager.get_setter_cmpt_mgr()
        insert_manager.return_value = \
            MockComponentManager.get_insert_pre_hourly_cmpt_mgr()

        # init mock driver tables
        data_driven_specs_repo.return_value = \
            MockDataDrivenSpecsRepo(self.spark_context,
                                    self.get_pre_transform_specs_json(),
                                    self.get_transform_specs_json_by_operation(
                                        test_operation))

        # Create an emulated set of Kafka messages (these were gathered
        # by extracting Monasca messages from the Metrics queue on mini-mon).

        # Create an RDD out of the mocked Monasca metrics
        with open(SecondStageDataProvider.fetch_quantity_data_path) as f:
            raw_lines = f.read().splitlines()
        raw_tuple_list = [eval(raw_line) for raw_line in raw_lines]
        instance_usage_list = map(dump_as_ascii_string,
                                  raw_tuple_list)

        # create a json RDD from instance_usage_list
        instance_usage_rdd = self.spark_context.parallelize(
            instance_usage_list)

        sql_context = SQLContext(self.spark_context)
        instance_usage_df = sql_context.read.json(
            instance_usage_rdd)

        # call pre hourly processor
        PreHourlyProcessor.do_transform(instance_usage_df)

        metrics = DummyAdapter.adapter_impl.metric_list
        mem_total_mb_agg_metric = [
            value for value in metrics
            if value.get('metric').get('name') ==
            'mem.total_mb_agg' and
            value.get('metric').get('dimensions').get('host') ==
            'mini-mon'][0]
        self.assertTrue(mem_total_mb_agg_metric is not None)

        self.assertEqual('mem.total_mb_agg',
                         mem_total_mb_agg_metric
                         .get('metric').get('name'))

        self.assertEqual(8679.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value'))
        self.assertEqual('useast',
                         mem_total_mb_agg_metric
                         .get('meta').get('region'))

        self.assertEqual(cfg.CONF.messaging.publish_kafka_project_id,
                         mem_total_mb_agg_metric
                         .get('meta').get('tenantId'))
        self.assertEqual('mini-mon',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('host'))
        self.assertEqual('all',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions').get('project_id'))
        self.assertEqual('prehourly',
                         mem_total_mb_agg_metric
                         .get('metric').get('dimensions')
                         .get('aggregation_period'))

        self.assertEqual(39.0,
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta').get('record_count'))
        self.assertEqual('2016-01-20 16:40:00',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('firstrecord_timestamp_string'))
        self.assertEqual('2016-01-20 17:40:00',
                         mem_total_mb_agg_metric
                         .get('metric').get('value_meta')
                         .get('lastrecord_timestamp_string'))


if __name__ == "__main__":
    print("PATH *************************************************************")
    import sys
    print(sys.path)
    print("PATH==============================================================")
    unittest.main()
