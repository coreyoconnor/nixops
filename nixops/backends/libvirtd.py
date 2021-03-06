# -*- coding: utf-8 -*-

from distutils import spawn
import os
import copy
import random
import shutil
import string
import subprocess
import sys
import time

from nixops.backends import MachineDefinition, MachineState
import nixops.known_hosts
import nixops.util


class LibvirtdDefinition(MachineDefinition):
    """Definition of a trivial machine."""

    @classmethod
    def get_type(cls):
        return "libvirtd"

    def __init__(self, xml, config):
        MachineDefinition.__init__(self, xml, config)

        x = xml.find("attrs/attr[@name='libvirtd']/attrs")
        assert x is not None
        self.vcpu = x.find("attr[@name='vcpu']/int").get("value")
        self.memory_size = x.find("attr[@name='memorySize']/int").get("value")
        self.extra_devices = x.find("attr[@name='extraDevicesXML']/string").get("value")
        self.extra_domain = x.find("attr[@name='extraDomainXML']/string").get("value")
        self.headless = x.find("attr[@name='headless']/bool").get("value") == 'true'
        self.image_dir = x.find("attr[@name='imageDir']/string").get("value")
        assert self.image_dir is not None

        self.networks = [
            k.get("value")
            for k in x.findall("attr[@name='networks']/list/string")]
        assert len(self.networks) > 0

        def parse_disk(xml):
            result = {
                'device': xml.find("attrs/attr[@name='device']/string").get("value"),
                'size': xml.find("attrs/attr[@name='size']/int").get("value"),
            }
            baseImageDefn = xml.find("attrs/attr[@name='baseImage']/string")
            if baseImageDefn is not None:
                result.baseImage = baseImageDefn.get("value")
            return result

        self.disks = { k.get("name"): parse_disk(k)
                       for k in x.findall("attr[@name='disks']/attrs/attr") }


class LibvirtdState(MachineState):
    private_ipv4 = nixops.util.attr_property("privateIpv4", None)
    client_public_key = nixops.util.attr_property("libvirtd.clientPublicKey", None)
    client_private_key = nixops.util.attr_property("libvirtd.clientPrivateKey", None)
    primary_net = nixops.util.attr_property("libvirtd.primaryNet", None)
    primary_mac = nixops.util.attr_property("libvirtd.primaryMAC", None)
    domain_xml = nixops.util.attr_property("libvirtd.domainXML", None)
    disk_path = nixops.util.attr_property("libvirtd.diskPath", None)
    extra_disks = nixops.util.attr_property("libvirtd.extraDisks", {}, 'json')
    vcpu = nixops.util.attr_property("libvirtd.vcpu", None)

    @classmethod
    def get_type(cls):
        return "libvirtd"

    def __init__(self, depl, name, id):
        MachineState.__init__(self, depl, name, id)

    def get_ssh_private_key_file(self):
        return self._ssh_private_key_file or self.write_ssh_private_key(self.client_private_key)

    def get_ssh_flags(self, *args, **kwargs):
        super_flags = super(LibvirtdState, self).get_ssh_flags(*args, **kwargs)
        return super_flags + ["-o", "StrictHostKeyChecking=no",
                              "-i", self.get_ssh_private_key_file()]

    def get_physical_spec(self):
        if not self.client_public_key:
            (self.client_private_key, self.client_public_key) = nixops.util.create_key_pair()
        return {('users', 'extraUsers', 'root', 'openssh', 'authorizedKeys', 'keys'): [self.client_public_key]}

    def address_to(self, m):
        if isinstance(m, LibvirtdState):
            return m.private_ipv4
        return MachineState.address_to(self, m)

    def _vm_id(self):
        return "nixops-{0}-{1}".format(self.depl.uuid, self.name)

    def _generate_primary_mac(self):
        mac = [0x52, 0x54, 0x00,
               random.randint(0x00, 0x7f),
               random.randint(0x00, 0xff),
               random.randint(0x00, 0xff)]
        self.primary_mac = ':'.join(map(lambda x: "%02x" % x, mac))

    def create(self, defn, check, allow_reboot, allow_recreate):
        assert isinstance(defn, LibvirtdDefinition)
        self.set_common_state(defn)
        self.primary_net = defn.networks[0]
        if not self.primary_mac:
            self._generate_primary_mac()

        if not self.client_public_key:
            (self.client_private_key, self.client_public_key) = nixops.util.create_key_pair()

        if self.vm_id is None:
            newEnv = copy.deepcopy(os.environ)
            newEnv["NIXOPS_LIBVIRTD_PUBKEY"] = self.client_public_key
            base_image = self._logged_exec(
                ["nix-build"] + self.depl._eval_flags(self.depl.nix_exprs) +
                ["--arg", "checkConfigurationOptions", "false",
                 "-A", "nodes.{0}.config.deployment.libvirtd.baseImage".format(self.name),
                 "-o", "{0}/libvirtd-image-{1}".format(self.depl.tempdir, self.name)],
                capture_stdout=True, env=newEnv).rstrip()

            if not os.access(defn.image_dir, os.W_OK):
                raise Exception('{} is not writable by this user or it does not exist'.format(defn.image_dir))

            self.disk_path = self._disk_path(defn)
            shutil.copyfile(base_image + "/disk.qcow2", self.disk_path)
            # Rebase onto empty backing file to prevent breaking the disk image
            # when the backing file gets garbage collected.
            self._logged_exec(["qemu-img", "rebase", "-f", "qcow2", "-b",
                               "", self.disk_path])
            os.chmod(self.disk_path, 0660)

            self.extra_disks = self._copy_extra_disks(defn)

            self.vm_id = self._vm_id()
            dom_file = self.depl.tempdir + "/{0}-domain.xml".format(self.name)
            self.domain_xml = self._make_domain_xml(defn)
            nixops.util.write_file(dom_file, self.domain_xml)
            # By using "virsh define" we ensure that the domain is
            # "persistent", as opposed to "transient" (removed on reboot).
            self._logged_exec(["virsh", "-c", "qemu:///system", "define", dom_file])
        self.start()
        return True

    def _disk_path(self, defn):
        return "{0}/{1}.img".format(defn.image_dir, self._vm_id())

    def _extra_disk_base_image_path(self, defn, disk_name, disk_defn):
        image_build = self._logged_exec(
            ["nix-build"] + self.depl._nix_path_flags() +
            ["<nixops/generate-ext4-image.nix>",
              "--arg", "size", disk_defn['size'],
              "--argstr", "name", disk_name,
              "-o", "{0}/libvirtd-image-{1}-{2}".format(self.depl.tempdir, self.name, disk_name)],
            capture_stdout=True).rstrip()
        return image_build + "/disk.qcow2"

    def _copy_extra_disks(self, defn):
        out = {}
        for disk_name, disk_defn in defn.disks.items():
            base_image_path = self._extra_disk_base_image_path(defn, disk_name, disk_defn)
            disk_path = "{0}/{1}-{2}.img".format(defn.image_dir, self._vm_id(), disk_name)
            self._logged_exec(["qemu-img", "create", "-f", "qcow2", "-b",
                               base_image_path, disk_path])
            out[disk_name] = {}
            out[disk_name]['device'] = disk_defn['device']
            out[disk_name]['imagePath'] = disk_path
        return out

    def _make_domain_xml(self, defn):
        qemu_executable = "qemu-system-x86_64"
        qemu = spawn.find_executable(qemu_executable)
        assert qemu is not None, "{} executable not found. Please install QEMU first.".format(qemu_executable)

        def maybe_mac(n):
            if n == self.primary_net:
                return '<mac address="' + self.primary_mac + '" />'
            else:
                return ""

        def iface(n):
            return "\n".join([
                '    <interface type="network">',
                maybe_mac(n),
                '      <source network="{0}"/>',
                '      <model type="virtio"/>',
                '    </interface>',
            ]).format(n)

        def disk_xml(disk_name, disk_state):
            return "\n".join([
                '    <disk type="file" device="disk">',
                '      <driver name="qemu" type="qcow2"/>',
                '      <source file="{0}"/>',
                '      <target dev="{1}"/>',
                '    </disk>',
            ]).format(disk_state['imagePath'], disk_state['device'])

        domain_fmt = "\n".join([
            '<domain type="kvm">',
            '  <name>{0}</name>',
            '  <memory unit="MiB">{1}</memory>',
            '  <vcpu>{4}</vcpu>',
            '  <cpu>',
            '    <topology sockets="1" cores="{4}" threads="1"/>',
            '  </cpu>',
            '  <os>',
            '    <type arch="x86_64">hvm</type>',
            '  </os>',
            '  <devices>',
            '    <emulator>{2}</emulator>',
            '    <disk type="file" device="disk">',
            '      <driver name="qemu" type="qcow2"/>',
            '      <source file="{3}"/>',
            '      <target dev="hda"/>',
            '    </disk>',
            '\n'.join([disk_xml(disk_name, disk_state) for disk_name, disk_state in self.extra_disks.items()]),
            '\n'.join([iface(n) for n in defn.networks]),
            '    <graphics type="sdl" display=":0.0"/>' if not defn.headless else "",
            '    <input type="keyboard" bus="usb"/>',
            '    <input type="mouse" bus="usb"/>',
            defn.extra_devices,
            '  </devices>',
            defn.extra_domain,
            '</domain>',
        ])

        return domain_fmt.format(
            self._vm_id(),
            defn.memory_size,
            qemu,
            self._disk_path(defn),
            defn.vcpu
        )

    def _parse_ip(self):
        cmd = [
            "virsh",
            "-c",
            "qemu:///system",
            "net-dhcp-leases",
            "--network",
            self.primary_net,
        ]
        lines = subprocess.check_output(cmd)
        try:
            i = lines.split().index(self.primary_mac)
        except ValueError:
            pass
        else:
            ip_with_subnet = lines.split()[i + 2]
            return ip_with_subnet.split('/')[0]

    def _wait_for_ip(self, prev_time):
        self.log_start("waiting for IP address to appear in DHCP leases...")
        while True:
            ip = self._parse_ip()
            if ip:
                self.private_ipv4 = ip
                break
            time.sleep(1)
            self.log_continue(".")
        self.log_end(" " + self.private_ipv4)

    def _is_running(self):
        ls = subprocess.check_output(["virsh", "-c", "qemu:///system", "list"])
        return (string.find(ls, self.vm_id) != -1)

    def start(self):
        assert self.vm_id
        assert self.domain_xml
        assert self.primary_net
        if self._is_running():
            self.log("connecting...")
            self.private_ipv4 = self._parse_ip()
        else:
            self.log("starting...")
            self._logged_exec(["virsh", "-c", "qemu:///system", "start", self.vm_id])
            self._wait_for_ip(0)

    def get_ssh_name(self):
        assert self.private_ipv4
        return self.private_ipv4

    def stop(self):
        assert self.vm_id
        if self._is_running():
            self.log_start("shutting down... ")
            self._logged_exec(["virsh", "-c", "qemu:///system", "destroy", self.vm_id])
        else:
            self.log("not running")
        self.state = self.STOPPED

    def destroy(self, wipe=False):
        if not self.vm_id:
            return True
        self.log_start("destroying... ")
        self.stop()
        self._logged_exec(["virsh", "-c", "qemu:///system", "undefine", self.vm_id])
        if (self.disk_path and os.path.exists(self.disk_path)):
            os.unlink(self.disk_path)
        for disk_name, disk_state in self.extra_disks.items():
            image_path = disk_state['imagePath']
            if (image_path and os.path.exists(image_path)):
                os.unlink(image_path)
        return True
