.. _datasource_aliyun:

Alibaba Cloud (AliYun)
**********************

The ``AliYun`` datasource reads data from Alibaba Cloud ECS. Support is
present in ``cloud-init`` since 0.7.9.

Instance metadata service
=========================

The Alibaba Cloud instance metadata service is available at the well known URL
:file:`http://100.100.100.200/`. For more information see Alibaba Cloud ECS
on `meta-data`_.

Configuration
=============

The following configuration can be set for the datasource in system
configuration (in :file:`/etc/cloud/cloud.cfg` or
:file:`/etc/cloud/cloud.cfg.d/`).

An example configuration with the default values is provided below:

.. code-block:: yaml

   datasource:
     AliYun:
       metadata_urls: ["http://100.100.100.200"]
       timeout: 50
       max_wait: 120

Versions
--------

Like the EC2 instance metadata service, Alibaba Cloud's instance metadata
service provides versioned data under specific paths. As of April 2018, there
are only ``2016-01-01`` and ``latest`` versions.

It is expected that the dated version will maintain a stable interface but
``latest`` may change content at a future date.

``Cloud-init`` uses the ``2016-01-01`` version.

You can list the versions available to your instance with:

.. code-block:: shell-session

   $ curl http://100.100.100.200/

Example output:

.. code-block::

   2016-01-01
   latest

Instance Metadata Service
-------------------------

The instance metadata service can be queried at
:file:`http://100.100.100.200/2016-01-01/meta-data`:

.. code-block:: shell-session

   $ curl http://100.100.100.200/2016-01-01/meta-data

Example output:

.. code-block::

   dns-conf/
   eipv4
   hostname
   image-id
   instance-id
   instance/
   mac
   network-type
   network/
   ntp-conf/
   owner-account-id
   private-ipv4
   public-keys/
   region-id
   serial-number
   source-address
   sub-private-ipv4-list
   vpc-cidr-block
   vpc-id

User-data
---------

If provided, user-data will appear at
:file:`http://100.100.100.200/2016-01-01/user-data`.
If no user-data is provided, this will return a 404.

.. code-block:: shell-session

   $ curl http://100.100.100.200/2016-01-01/user-data

Example output:

.. code-block::

   #!/bin/sh
   echo "Hello World."

.. LINKS
.. _meta-data: https://www.alibabacloud.com/help/zh/faq-detail/49122.htm
