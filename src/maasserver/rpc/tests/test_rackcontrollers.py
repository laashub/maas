# Copyright 2016 Canonical Ltd. This software is licnesed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for :py:module;`maasserver.rpc.rackcontroller`."""

__all__ = []

import random
from unittest.mock import sentinel

from fixtures import FakeLogger
from maasserver import (
    locks,
    worker_user,
)
from maasserver.enum import (
    INTERFACE_TYPE,
    IPADDRESS_TYPE,
    NODE_TYPE,
)
from maasserver.models import (
    Node,
    NodeGroupToRackController,
    RackController,
)
from maasserver.models.interface import PhysicalInterface
from maasserver.models.timestampedmodel import now
from maasserver.rpc import rackcontrollers
from maasserver.rpc.rackcontrollers import (
    handle_upgrade,
    register,
    update_foreign_dhcp,
    update_interfaces,
    update_last_image_sync,
)
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.utils.orm import reload_object
from maastesting.matchers import MockCalledOnceWith
from testtools.matchers import (
    IsInstance,
    MatchesAll,
    MatchesSetwise,
    MatchesStructure,
)


class TestHandleUpgrade(MAASServerTestCase):

    def test_migrates_nodegroup_subnet(self):
        rack = factory.make_RackController()
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        ip = factory.pick_ip_in_Subnet(subnet)
        interfaces = {
            factory.make_name("eth0"): {
                "type": "physical",
                "mac_address": factory.make_mac_address(),
                "parents": [],
                "links": [{
                    "mode": "static",
                    "address": "%s/%d" % (
                        str(ip), subnet.get_ipnetwork().prefixlen),
                }],
                "enabled": True,
            }
        }
        rack.update_interfaces(interfaces)
        ng_uuid = factory.make_UUID()
        NodeGroupToRackController.objects.create(uuid=ng_uuid, subnet=subnet)
        handle_upgrade(rack, ng_uuid)
        vlan = reload_object(vlan)
        self.assertEqual(rack.system_id, vlan.primary_rack.system_id)
        self.assertTrue(vlan.dhcp_on)
        self.assertItemsEqual([], NodeGroupToRackController.objects.all())

    def test_logs_migration(self):
        logger = self.useFixture(FakeLogger("maas"))
        rack = factory.make_RackController()
        vlan = factory.make_VLAN()
        subnet = factory.make_Subnet(vlan=vlan)
        ip = factory.pick_ip_in_Subnet(subnet)
        interfaces = {
            factory.make_name("eth0"): {
                "type": "physical",
                "mac_address": factory.make_mac_address(),
                "parents": [],
                "links": [{
                    "mode": "static",
                    "address": "%s/%d" % (
                        str(ip), subnet.get_ipnetwork().prefixlen),
                }],
                "enabled": True,
            }
        }
        rack.update_interfaces(interfaces)
        ng_uuid = factory.make_UUID()
        NodeGroupToRackController.objects.create(uuid=ng_uuid, subnet=subnet)
        handle_upgrade(rack, ng_uuid)
        vlan = reload_object(vlan)
        self.assertEqual(
            "DHCP setting from NodeGroup(%s) have been migrated to %s." % (
                ng_uuid, vlan),
            logger.output.strip())


class TestRegisterRackController(MAASServerTestCase):

    def test_sets_owner_to_worker_when_none(self):
        node = factory.make_Node()
        rack_registered, needs_refresh = register(system_id=node.system_id)
        self.assertEqual(worker_user.get_worker_user(), rack_registered.owner)

    def test_leaves_owner_when_owned(self):
        user = factory.make_User()
        node = factory.make_Machine(owner=user)
        rack_registered, needs_refresh = register(system_id=node.system_id)
        self.assertEqual(user, rack_registered.owner)

    def test_finds_existing_node_by_system_id(self):
        node = factory.make_Node()
        rack_registered, needs_refresh = register(system_id=node.system_id)
        self.assertEqual(node.system_id, rack_registered.system_id)

    def test_finds_existing_node_by_hostname(self):
        node = factory.make_Node()
        rack_registered, needs_refresh = register(hostname=node.hostname)
        self.assertEqual(node.system_id, rack_registered.system_id)

    def test_finds_existing_node_by_mac(self):
        node = factory.make_Node()
        nic = factory.make_Interface(node=node)
        mac = nic.mac_address.raw
        interfaces = {
            nic.name: {
                "type": "physical",
                "mac_address": mac,
                "parents": [],
                "links": [],
                "enabled": True,
            }
        }
        rack_registered, needs_refresh = register(interfaces=interfaces)
        self.assertEqual(node.system_id, rack_registered.system_id)

    def test_finds_existing_controller_needs_refresh_with_bad_info(self):
        node_type = random.choice([
            NODE_TYPE.RACK_CONTROLLER,
            NODE_TYPE.REGION_AND_RACK_CONTROLLER,
        ])
        node = factory.make_Node(node_type=node_type)
        rack_registered, needs_refresh = register(system_id=node.system_id)
        self.assertTrue(needs_refresh)

    def test_finds_existing_controller_doesnt_need_refresh_good_info(self):
        node_type = random.choice([
            NODE_TYPE.RACK_CONTROLLER,
            NODE_TYPE.REGION_AND_RACK_CONTROLLER,
        ])
        node = factory.make_Node(
            node_type=node_type, cpu_count=random.randint(1, 32),
            memory=random.randint(1024, 8096))
        rack_registered, needs_refresh = register(system_id=node.system_id)
        self.assertFalse(needs_refresh)

    def test_converts_existing_node_sets_needs_refresh_to_true(self):
        node_type = random.choice([
            NODE_TYPE.MACHINE,
            NODE_TYPE.DEVICE,
            NODE_TYPE.REGION_CONTROLLER,
        ])
        node = factory.make_Node(node_type=node_type)
        rack_registered, needs_refresh = register(system_id=node.system_id)
        self.assertTrue(needs_refresh)

    def test_find_existing_keeps_type(self):
        node_type = random.choice(
            (NODE_TYPE.RACK_CONTROLLER, NODE_TYPE.REGION_AND_RACK_CONTROLLER))
        node = factory.make_Node(node_type=node_type)
        register(system_id=node.system_id)
        self.assertEqual(node_type, node.node_type)

    def test_logs_finding_existing_node(self):
        logger = self.useFixture(FakeLogger("maas"))
        node = factory.make_Node(node_type=NODE_TYPE.RACK_CONTROLLER)
        register(system_id=node.system_id)
        self.assertEqual(
            "Registering existing rack controller %s." % node.hostname,
            logger.output.strip())

    def test_converts_region_controller(self):
        node = factory.make_Node(node_type=NODE_TYPE.REGION_CONTROLLER)
        rack_registered, needs_refresh = register(system_id=node.system_id)
        self.assertEqual(
            rack_registered.node_type, NODE_TYPE.REGION_AND_RACK_CONTROLLER)

    def test_logs_converting_region_controller(self):
        logger = self.useFixture(FakeLogger("maas"))
        node = factory.make_Node(node_type=NODE_TYPE.REGION_CONTROLLER)
        register(system_id=node.system_id)
        self.assertEqual(
            "Found existing node %s as candidate for rack controller.\n"
            "Converting %s into a region and rack controller.\n"
            % (node.hostname, node.hostname),
            logger.output)

    def test_converts_existing_node(self):
        node = factory.make_Node(node_type=NODE_TYPE.MACHINE)
        rack_registered, needs_refresh = register(system_id=node.system_id)
        self.assertEqual(rack_registered.node_type, NODE_TYPE.RACK_CONTROLLER)

    def test_logs_converting_existing_node(self):
        logger = self.useFixture(FakeLogger("maas"))
        node = factory.make_Node(node_type=NODE_TYPE.MACHINE)
        register(system_id=node.system_id)
        self.assertEqual(
            "Found existing node %s as candidate for rack controller.\n"
            "Converting %s into a rack controller.\n"
            % (node.hostname, node.hostname),
            logger.output)

    def test_creates_new_rackcontroller(self):
        factory.make_Node()
        node_count = len(Node.objects.all())
        interfaces = {
            factory.make_name("eth0"): {
                "type": "physical",
                "mac_address": factory.make_mac_address(),
                "parents": [],
                "links": [],
                "enabled": True,
            }
        }
        register(interfaces=interfaces)
        self.assertEqual(node_count + 1, len(Node.objects.all()))

    def test_creates_new_rackcontroller_sets_needs_refresh_to_true(self):
        interfaces = {
            factory.make_name("eth0"): {
                "type": "physical",
                "mac_address": factory.make_mac_address(),
                "parents": [],
                "links": [],
                "enabled": True,
            }
        }
        rack_registered, needs_refresh = register(interfaces=interfaces)
        self.assertTrue(needs_refresh)

    def test_logs_creating_new_rackcontroller(self):
        logger = self.useFixture(FakeLogger("maas"))
        hostname = factory.make_name("hostname")
        register(hostname=hostname)
        self.assertEqual(
            "Created new rack controller %s." % hostname,
            logger.output.strip())

    def test_sets_interfaces(self):
        # Interfaces are set on new rack controllers.
        interfaces = {
            factory.make_name("eth0"): {
                "type": "physical",
                "mac_address": factory.make_mac_address(),
                "parents": [],
                "links": [],
                "enabled": True,
            }
        }
        rack_registered, needs_refresh = register(interfaces=interfaces)
        self.assertThat(
            rack_registered.interface_set.all(),
            MatchesSetwise(*(
                MatchesAll(
                    IsInstance(PhysicalInterface),
                    MatchesStructure.byEquality(
                        name=name, mac_address=interface["mac_address"],
                        enabled=interface["enabled"],
                    ),
                    first_only=True,
                )
                for name, interface in interfaces.items()
            )))

    def test_updates_interfaces(self):
        # Interfaces are set on existing rack controllers.
        rack_controller = factory.make_RackController()
        interfaces = {
            factory.make_name("eth0"): {
                "type": "physical",
                "mac_address": factory.make_mac_address(),
                "parents": [],
                "links": [],
                "enabled": True,
            }
        }
        rack_registered, needs_refresh = register(
            rack_controller.system_id, interfaces=interfaces)
        self.assertThat(
            rack_registered.interface_set.all(),
            MatchesSetwise(*(
                MatchesAll(
                    IsInstance(PhysicalInterface),
                    MatchesStructure.byEquality(
                        name=name, mac_address=interface["mac_address"],
                        enabled=interface["enabled"],
                    ),
                    first_only=True,
                )
                for name, interface in interfaces.items()
            )))

    def test_registers_with_rack_registration_lock_held(self):
        lock_status = []

        def record_lock_status(*args):
            lock_status.append(locks.rack_registration.is_locked())
            return None  # Simulate that no rack found.

        find = self.patch(rackcontrollers, "find")
        find.side_effect = record_lock_status

        register()

        self.assertEqual([True], lock_status)


class TestUpdateForeignDHCP(MAASServerTestCase):

    def test__doesnt_fail_if_interface_missing(self):
        rack_controller = factory.make_RackController()
        # No error should be raised.
        update_foreign_dhcp(
            rack_controller.system_id, factory.make_name("eth"), None)

    def test__clears_external_dhcp_on_vlan(self):
        rack_controller = factory.make_RackController(interface=False)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller)
        interface.vlan.external_dhcp = factory.make_ip_address()
        interface.vlan.save()
        update_foreign_dhcp(
            rack_controller.system_id, interface.name, None)
        self.assertIsNone(reload_object(interface.vlan).external_dhcp)

    def test__sets_external_dhcp_when_not_managed_vlan(self):
        rack_controller = factory.make_RackController(interface=False)
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller)
        dhcp_ip = factory.make_ip_address()
        update_foreign_dhcp(
            rack_controller.system_id, interface.name, dhcp_ip)
        self.assertEquals(
            dhcp_ip, reload_object(interface.vlan).external_dhcp)

    def test__clears_external_dhcp_when_managed_vlan(self):
        rack_controller = factory.make_RackController(interface=False)
        fabric = factory.make_Fabric()
        vlan = fabric.get_default_vlan()
        interface = factory.make_Interface(
            INTERFACE_TYPE.PHYSICAL, node=rack_controller, vlan=vlan)
        subnet = factory.make_Subnet()
        dhcp_ip = factory.pick_ip_in_Subnet(subnet)
        vlan.dhcp_on = True
        vlan.primary_rack = rack_controller
        vlan.external_dhcp = dhcp_ip
        vlan.save()
        factory.make_StaticIPAddress(
            alloc_type=IPADDRESS_TYPE.STICKY, ip=dhcp_ip,
            subnet=subnet, interface=interface)
        update_foreign_dhcp(
            rack_controller.system_id, interface.name, dhcp_ip)
        self.assertIsNone(reload_object(interface.vlan).external_dhcp)


class TestUpdateInterfaces(MAASServerTestCase):

    def test__calls_update_interfaces_on_rack_controller(self):
        rack_controller = factory.make_RackController()
        patched_update_interfaces = self.patch(
            RackController, "update_interfaces")
        update_interfaces(rack_controller.system_id, sentinel.interfaces)
        self.assertThat(
            patched_update_interfaces,
            MockCalledOnceWith(sentinel.interfaces))


class TestUpdateLastImageSync(MAASServerTestCase):

    def test__updates_last_image_sync(self):
        rack = factory.make_RackController()

        update_last_image_sync(rack.system_id)

        self.assertEqual(now(), reload_object(rack).last_image_sync)