# Copyright (C) 2009-2010 Canonical Ltd.
# Copyright (C) 2012 Hewlett-Packard Development Company, L.P.
#
# Author: Scott Moser <scott.moser@canonical.com>
# Author: Juerg Haefliger <juerg.haefliger@hp.com>
#
# This file is part of cloud-init. See LICENSE file for license information.

"""
Puppet
------
**Summary:** install, configure and start puppet

This module handles puppet installation and configuration. If the ``puppet``
key does not exist in global configuration, no action will be taken. If a
config entry for ``puppet`` is present, then by default the latest version of
puppet will be installed. If ``install`` is set to ``false``, puppet will not
be installed. However, this will result in an error if puppet is not already
present on the system. The version of puppet to be installed can be specified
under ``version``, and defaults to ``none``, which selects the latest version
in the repos. If the ``puppet`` config key exists in the config archive, this
module will attempt to start puppet even if no installation was performed.

The module also provides keys for configuring the new puppet 4 paths and
installing the puppet package from the puppetlabs repositories:
https://docs.puppet.com/puppet/4.2/reference/whered_it_go.html
The keys are ``package_name``, ``conf_file``, ``ssl_dir`` and
``csr_attributes_path``. If unset, their values will default to
ones that work with puppet 3.x and with distributions that ship modified
puppet 4.x that uses the old paths.

Agent packages from the puppetlabs repositories can be installed by setting
``install_type`` to ``aio``. Based on this setting, the default config/SSL/CSR
paths will be adjusted accordingly. To maintain backwards compatibility this
setting defaults to ``packages`` which will install puppet from the distro
packages.

If installing ``aio`` packages, ``collection`` can also be set to one of
``puppet`` (rolling release), ``puppet6``, ``puppet7`` (or their nightly
counterparts) in order to install specific release streams. By default, the
puppetlabs repository will be purged after installation finishes; set
``cleanup`` to ``false`` to prevent this. AIO packages are installed through a
shell script which is downloaded on the machine and then executed; the path to
this script can be overridden using the ``aio_install_url`` key.

Puppet configuration can be specified under the ``conf`` key. The
configuration is specified as a dictionary containing high-level ``<section>``
keys and lists of ``<key>=<value>`` pairs within each section. Each section
name and ``<key>=<value>`` pair is written directly to ``puppet.conf``. As
such,  section names should be one of: ``main``, ``server``, ``agent`` or
``user`` and keys should be valid puppet configuration options. The
``certname`` key supports string substitutions for ``%i`` and ``%f``,
corresponding to the instance id and fqdn of the machine respectively.
If ``ca_cert`` is present, it will not be written to ``puppet.conf``, but
instead will be used as the puppetserver certificate. It should be specified
in pem format as a multi-line string (using the ``|`` yaml notation).

Additionally it's possible to create a ``csr_attributes.yaml`` file for CSR
attributes and certificate extension requests.
See https://puppet.com/docs/puppet/latest/config_file_csr_attributes.html

The puppet service will be automatically enabled after installation. A manual
run can also be triggered by setting ``exec`` to ``true``, and additional
arguments can be passed to ``puppet agent`` via the ``exec_args`` key (by
default the agent will execute with the ``--test`` flag).

**Internal name:** ``cc_puppet``

**Module frequency:** per instance

**Supported distros:** all

**Config keys**::

    puppet:
        install: <true/false>
        version: <version>
        collection: <aio collection>
        install_type: <packages/aio>
        aio_install_url: 'https://git.io/JBhoQ'
        cleanup: <true/false>
        conf_file: '/etc/puppet/puppet.conf'
        ssl_dir: '/var/lib/puppet/ssl'
        csr_attributes_path: '/etc/puppet/csr_attributes.yaml'
        package_name: 'puppet'
        exec: <true/false>
        exec_args: ['--test']
        conf:
            agent:
                server: "puppetserver.example.org"
                certname: "%i.%f"
                ca_cert: |
                    -------BEGIN CERTIFICATE-------
                    <cert data>
                    -------END CERTIFICATE-------
        csr_attributes:
            custom_attributes:
                1.2.840.113549.1.9.7: 342thbjkt82094y0uthhor289jnqthpc2290
            extension_requests:
                pp_uuid: ED803750-E3C7-44F5-BB08-41A04433FE2E
                pp_image_name: my_ami_image
                pp_preshared_key: 342thbjkt82094y0uthhor289jnqthpc2290
"""

import os
import socket
import yaml
from io import StringIO

from cloudinit import helpers
from cloudinit import subp
from cloudinit import temp_utils
from cloudinit import util
from cloudinit import url_helper

AIO_INSTALL_URL = 'https://raw.githubusercontent.com/puppetlabs/install-puppet/main/install.sh'  # noqa: E501
PUPPET_AGENT_DEFAULT_ARGS = ['--test']


class PuppetConstants(object):

    def __init__(self, puppet_conf_file, puppet_ssl_dir,
                 csr_attributes_path, log):
        self.conf_path = puppet_conf_file
        self.ssl_dir = puppet_ssl_dir
        self.ssl_cert_dir = os.path.join(puppet_ssl_dir, "certs")
        self.ssl_cert_path = os.path.join(self.ssl_cert_dir, "ca.pem")
        self.csr_attributes_path = csr_attributes_path


def _autostart_puppet(log):
    # Set puppet to automatically start
    if os.path.exists('/etc/default/puppet'):
        subp.subp(['sed', '-i',
                   '-e', 's/^START=.*/START=yes/',
                   '/etc/default/puppet'], capture=False)
    elif os.path.exists('/bin/systemctl'):
        subp.subp(['/bin/systemctl', 'enable', 'puppet.service'],
                  capture=False)
    elif os.path.exists('/sbin/chkconfig'):
        subp.subp(['/sbin/chkconfig', 'puppet', 'on'], capture=False)
    else:
        log.warning(("Sorry we do not know how to enable"
                     " puppet services on this system"))


def get_config_value(puppet_bin, setting):
    """Get the config value for a given setting using `puppet config print`
    :param puppet_bin: path to puppet binary
    :param setting: setting to query
    """
    out, _ = subp.subp([puppet_bin, 'config', 'print', setting])
    return out.rstrip()


def install_puppet_aio(url=AIO_INSTALL_URL, version=None,
                       collection=None, cleanup=True):
    """Install puppet-agent from the puppetlabs repositories using the one-shot
    shell script

    :param url: URL from where to download the install script
    :param version: version to install, blank defaults to latest
    :param collection: collection to install, blank defaults to latest
    :param cleanup: whether to purge the puppetlabs repo after installation
    """
    args = []
    if version is not None:
        args = ['-v', version]
    if collection is not None:
        args += ['-c', collection]

    # Purge puppetlabs repos after installation
    if cleanup:
        args += ['--cleanup']
    content = url_helper.readurl(url=url, retries=5).contents

    # Use tmpdir over tmpfile to avoid 'text file busy' on execute
    with temp_utils.tempdir(needs_exe=True) as tmpd:
        tmpf = os.path.join(tmpd, 'puppet-install')
        util.write_file(tmpf, content, mode=0o700)
        return subp.subp([tmpf] + args, capture=False)


def handle(name, cfg, cloud, log, _args):
    # If there isn't a puppet key in the configuration don't do anything
    if 'puppet' not in cfg:
        log.debug(("Skipping module named %s,"
                   " no 'puppet' configuration found"), name)
        return

    puppet_cfg = cfg['puppet']
    # Start by installing the puppet package if necessary...
    install = util.get_cfg_option_bool(puppet_cfg, 'install', True)
    version = util.get_cfg_option_str(puppet_cfg, 'version', None)
    collection = util.get_cfg_option_str(puppet_cfg, 'collection', None)
    install_type = util.get_cfg_option_str(
        puppet_cfg, 'install_type', 'packages')
    cleanup = util.get_cfg_option_bool(puppet_cfg, 'cleanup', True)
    run = util.get_cfg_option_bool(puppet_cfg, 'exec', default=False)
    aio_install_url = util.get_cfg_option_str(
        puppet_cfg, 'aio_install_url', default=AIO_INSTALL_URL)

    # AIO and distro packages use different paths
    if install_type == 'aio':
        puppet_user = 'root'
        puppet_bin = '/opt/puppetlabs/bin/puppet'
        puppet_package = 'puppet-agent'
    else:  # default to 'packages'
        puppet_user = 'puppet'
        puppet_bin = 'puppet'
        puppet_package = 'puppet'

    package_name = util.get_cfg_option_str(
        puppet_cfg, 'package_name', puppet_package)
    if not install and version:
        log.warning(("Puppet install set to false but version supplied,"
                     " doing nothing."))
    elif install:
        log.debug(("Attempting to install puppet %s from %s"),
                  version if version else 'latest', install_type)

        if install_type == "packages":
            cloud.distro.install_packages((package_name, version))
        elif install_type == "aio":
            install_puppet_aio(aio_install_url, version, collection, cleanup)
        else:
            log.warning("Unknown puppet install type '%s'", install_type)
            run = False

    conf_file = util.get_cfg_option_str(
        puppet_cfg, 'conf_file', get_config_value(puppet_bin, 'config'))
    ssl_dir = util.get_cfg_option_str(
        puppet_cfg, 'ssl_dir', get_config_value(puppet_bin, 'ssldir'))
    csr_attributes_path = util.get_cfg_option_str(
        puppet_cfg, 'csr_attributes_path',
        get_config_value(puppet_bin, 'csr_attributes'))

    p_constants = PuppetConstants(conf_file, ssl_dir, csr_attributes_path, log)

    # ... and then update the puppet configuration
    if 'conf' in puppet_cfg:
        # Add all sections from the conf object to puppet.conf
        contents = util.load_file(p_constants.conf_path)
        # Create object for reading puppet.conf values
        puppet_config = helpers.DefaultingConfigParser()
        # Read puppet.conf values from original file in order to be able to
        # mix the rest up. First clean them up
        # (TODO(harlowja) is this really needed??)
        cleaned_lines = [i.lstrip() for i in contents.splitlines()]
        cleaned_contents = '\n'.join(cleaned_lines)
        # Move to puppet_config.read_file when dropping py2.7
        puppet_config.read_file(
            StringIO(cleaned_contents),
            source=p_constants.conf_path)
        for (cfg_name, cfg) in puppet_cfg['conf'].items():
            # Cert configuration is a special case
            # Dump the puppetserver ca certificate in the correct place
            if cfg_name == 'ca_cert':
                # Puppet ssl sub-directory isn't created yet
                # Create it with the proper permissions and ownership
                util.ensure_dir(p_constants.ssl_dir, 0o771)
                util.chownbyname(p_constants.ssl_dir, puppet_user, 'root')
                util.ensure_dir(p_constants.ssl_cert_dir)

                util.chownbyname(p_constants.ssl_cert_dir, puppet_user, 'root')
                util.write_file(p_constants.ssl_cert_path, cfg)
                util.chownbyname(p_constants.ssl_cert_path,
                                 puppet_user, 'root')
            else:
                # Iterate through the config items, we'll use ConfigParser.set
                # to overwrite or create new items as needed
                for (o, v) in cfg.items():
                    if o == 'certname':
                        # Expand %f as the fqdn
                        # TODO(harlowja) should this use the cloud fqdn??
                        v = v.replace("%f", socket.getfqdn())
                        # Expand %i as the instance id
                        v = v.replace("%i", cloud.get_instance_id())
                        # certname needs to be downcased
                        v = v.lower()
                    puppet_config.set(cfg_name, o, v)
            # We got all our config as wanted we'll rename
            # the previous puppet.conf and create our new one
            util.rename(p_constants.conf_path, "%s.old"
                        % (p_constants.conf_path))
            util.write_file(p_constants.conf_path, puppet_config.stringify())

    if 'csr_attributes' in puppet_cfg:
        util.write_file(p_constants.csr_attributes_path,
                        yaml.dump(puppet_cfg['csr_attributes'],
                                  default_flow_style=False))

    # Set it up so it autostarts
    _autostart_puppet(log)

    # Run the agent if needed
    if run:
        log.debug('Running puppet-agent')
        cmd = [puppet_bin, 'agent']
        if 'exec_args' in puppet_cfg:
            cmd_args = puppet_cfg['exec_args']
            if isinstance(cmd_args, (list, tuple)):
                cmd.extend(cmd_args)
            elif isinstance(cmd_args, str):
                cmd.extend(cmd_args.split())
            else:
                log.warning("Unknown type %s provided for puppet"
                            " 'exec_args' expected list, tuple,"
                            " or string", type(cmd_args))
                cmd.extend(PUPPET_AGENT_DEFAULT_ARGS)
        else:
            cmd.extend(PUPPET_AGENT_DEFAULT_ARGS)
        subp.subp(cmd, capture=False)

    # Start puppetd
    subp.subp(['service', 'puppet', 'start'], capture=False)

# vi: ts=4 expandtab
