# Copyright 2014 Orange
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import collections
import functools
import logging as python_logging
import select
import time

from exabgp.bgp import fsm as exa_fsm
from exabgp.bgp import message as exa_message
from exabgp.bgp.message import open as exa_open
from exabgp.bgp import neighbor as exa_neighbor
from exabgp import logger as exa_logger
from exabgp.protocol import family as exa_family
from exabgp import reactor as exa_reactor
from exabgp.reactor import peer as exa_peer
from oslo_config import cfg
from oslo_log import log as logging

from networking_bagpipe.bagpipe_bgp.common import looking_glass as lg
from networking_bagpipe.bagpipe_bgp import engine
from networking_bagpipe.bagpipe_bgp.engine import bgp_peer_worker
from networking_bagpipe.bagpipe_bgp.engine import exa


LOG = logging.getLogger(__name__)


def setup_exabgp_env():
    # initialize/tweak ExaBGP config and log internals

    from exabgp.configuration.setup import environment
    environment.application = 'bagpipe-bgp'
    env = environment.setup(None)
    # tell exabgp to parse routes:
    env.log.routes = True

    # we "tweak" the internals of exabgp Logger, so that (a) it does not break
    # oslo_log and (b) it logs through oslo_log
    # decorating the original restart would be better...
    exa_logger.Logger._restart = exa_logger.Logger.restart

    def decorated_restart(f):
        @functools.wraps(f)
        def restart_never_first(self, first):
            # we don't want exabgp to really ever do its first restart stuff
            # that resets the root logger handlers
            return f(self, False)
        return restart_never_first

    exa_logger.Logger.restart = decorated_restart(
        exa_logger.Logger.restart
    )

    exa_logger.Logger._syslog = logging.getLogger(__name__ + ".exabgp").logger

    # prevent exabgp Logger code from adding or removing handlers for
    # this logger
    def noop(handler):
        pass

    exa_logger.Logger._syslog.addHandler = noop
    exa_logger.Logger._syslog.removeHandler = noop

    def patched_format(self, message, source, level, timestamp=None):
        if self.short:
            return message
        return "%-13s %s" % (source, message)

    exa_logger.Logger._format = patched_format

    env.log.enable = True

    if LOG.logger.getEffectiveLevel():
        env.log.level = environment.syslog_value(
            python_logging.getLevelName(LOG.logger.getEffectiveLevel())
        )
    else:
        env.log.level = environment.syslog_value('INFO')
    env.log.all = True
    env.log.packets = True

    # monkey patch exabgp to work around exabgp issue #690
    # only relevant for exabgp 4.0.2
    # ( https://github.com/Exa-Networks/exabgp/issues/690 )
    if hasattr(exa_family.Family, 'size'):
        exa_family.Family.size[(exa_family.AFI.l2vpn,
                                exa_family.SAFI.evpn)] = ((4,), 0)


TRANSLATE_EXABGP_STATE = {exa_fsm.FSM.IDLE: bgp_peer_worker.FSM.Idle,
                          exa_fsm.FSM.ACTIVE: bgp_peer_worker.FSM.Active,
                          exa_fsm.FSM.CONNECT: bgp_peer_worker.FSM.Connect,
                          exa_fsm.FSM.OPENSENT: bgp_peer_worker.FSM.OpenSent,
                          exa_fsm.FSM.OPENCONFIRM:
                              bgp_peer_worker.FSM.OpenConfirm,
                          exa_fsm.FSM.ESTABLISHED:
                              bgp_peer_worker.FSM.Established,
                          }


class ExaBGPPeerWorker(bgp_peer_worker.BGPPeerWorker, lg.LookingGlassMixin):

    enabled_families = [(exa.AFI.ipv4, exa.SAFI.mpls_vpn),
                        # (exa.exa.AFI.ipv6, exa.SAFI.mpls_vpn),
                        (exa.AFI.l2vpn, exa.SAFI.evpn),
                        (exa.AFI.ipv4, exa.SAFI.flow_vpn)]

    def __init__(self, bgp_manager, peer_address):
        bgp_peer_worker.BGPPeerWorker.__init__(self, bgp_manager, peer_address)

        self.local_address = cfg.CONF.BGP.local_address
        self.peer_address = peer_address

        self.peer = None

        self.rtc_active = False
        self._active_families = []

    # hooks into BGPPeerWorker state changes

    def _stop_and_clean(self):
        super()._stop_and_clean()

        self._active_families = []

        if self.peer is not None:
            self.log.info("Clearing peer")
            if self.peer.proto:
                self.peer.proto.close()
            self.peer.stop()
            self.peer = None

    def _to_established(self):
        super()._to_established()

        if self.rtc_active:
            self.log.debug("RTC active, subscribing to all RTC routes")
            # subscribe to RTC routes, to be able to propagate them from
            # internal workers to this peer
            self._subscribe(exa.AFI.ipv4, exa.SAFI.rtc)
        else:
            self.log.debug("RTC inactive, subscribing to all active families")
            # if we don't use RTC with our peer, then we need to see events for
            # all routes of all active families, to be able to send them to him
            for (afi, safi) in self._active_families:
                self._subscribe(afi, safi)

    # implementation of BGPPeerWorker abstract methods

    def _initiate_connection(self):
        self.log.debug("Initiate ExaBGP connection to %s:%s from %s",
                       self.peer_address, cfg.CONF.BGP.bgp_port,
                       self.local_address)

        self.rtc_active = False

        neighbor = exa_neighbor.Neighbor()
        neighbor.make_rib()
        neighbor.router_id = exa_open.RouterID(self.local_address)
        neighbor.local_as = exa.ASN(cfg.CONF.BGP.my_as)
        # no support for eBGP yet:
        neighbor.peer_as = exa.ASN(cfg.CONF.BGP.my_as)
        neighbor.local_address = exa.IP.create(self.local_address)
        neighbor.md5_ip = exa.IP.create(self.local_address)
        neighbor.peer_address = exa.IP.create(self.peer_address)
        neighbor.hold_time = exa_open.HoldTime(
            bgp_peer_worker.DEFAULT_HOLDTIME)
        neighbor.connect = cfg.CONF.BGP.bgp_port
        neighbor.api = collections.defaultdict(list)
        neighbor.extended_message = False

        for afi_safi in self.enabled_families:
            neighbor.add_family(afi_safi)

        if cfg.CONF.BGP.enable_rtc:
            # how to test fot this ?
            neighbor.add_family((exa.AFI.ipv4, exa.SAFI.rtc))

        self.log.debug("Instantiate ExaBGP Peer")
        self.peer = exa_peer.Peer(neighbor, None)

        try:
            for action in self.peer._establish():
                self.fsm.state = TRANSLATE_EXABGP_STATE[
                    self.peer.fsm.state]

                if action == exa_peer.ACTION.LATER:
                    time.sleep(2)
                elif action == exa_peer.ACTION.NOW:
                    time.sleep(0.1)

                if self.should_stop:
                    self.log.debug("We're closing, raise StoppedException")
                    raise bgp_peer_worker.StoppedException()

                if action == exa_peer.ACTION.CLOSE:
                    self.log.debug("Socket status is CLOSE, "
                                   "raise InitiateConnectionException")
                    raise bgp_peer_worker.InitiateConnectionException(
                        "Socket is closed")
        except exa_peer.Interrupted:
            self.log.debug("Connect was interrupted, "
                           "raise InitiateConnectionException")
            raise bgp_peer_worker.InitiateConnectionException(
                "Connect was interrupted")
        except exa_message.Notify as e:
            self.log.debug("Notify: %s", e)
            if (e.code, e.subcode) == (1, 1):
                raise bgp_peer_worker.OpenWaitTimeout(str(e))
            else:
                raise Exception("Notify received: %s" % e)
        except exa_reactor.network.error.LostConnection:
            raise

        # check the capabilities of the session just established...

        self.protocol = self.peer.proto

        received_open = self.protocol.negotiated.received_open

        self._set_hold_time(self.protocol.negotiated.holdtime)

        mp_capabilities = received_open.capabilities.get(
            exa_open.capability.Capability.CODE.MULTIPROTOCOL, [])

        # check that our peer advertized at least mpls_vpn and evpn
        # capabilities
        self._active_families = []
        for (afi, safi) in (self.__class__.enabled_families +
                            [(exa.AFI.ipv4, exa.SAFI.rtc)]):
            if (afi, safi) not in mp_capabilities:
                if (((afi, safi) != (exa.AFI.ipv4, exa.SAFI.rtc)) or
                        cfg.CONF.BGP.enable_rtc):
                    self.log.warning("Peer does not advertise (%s,%s) "
                                     "capability", afi, safi)
            else:
                self.log.info(
                    "Family (%s,%s) successfully negotiated with peer %s",
                    afi, safi, self.peer_address)
                self._active_families.append((afi, safi))

        if len(self._active_families) == 0:
            self.log.error("No family was negotiated for VPN routes")

        self.rtc_active = False

        if cfg.CONF.BGP.enable_rtc:
            if (exa.AFI.ipv4, exa.SAFI.rtc) in mp_capabilities:
                self.log.info(
                    "RTC successfully enabled with peer %s", self.peer_address)
                self.rtc_active = True
            else:
                self.log.warning(
                    "enable_rtc True but peer not configured for RTC")

    def _receive_loop_fun(self):

        try:
            select.select([self.protocol.connection.io], [], [], 2)

            if not self.protocol.connection:
                raise Exception("lost connection")

            message = next(self.protocol.read_message())

            if message.ID != exa_message.NOP.ID:
                self.log.debug("protocol read message: %s", message)
        except exa_message.Notification as e:
            self.log.error("Notification: %s", e)
            return 2
        except exa_reactor.network.error.LostConnection as e:
            self.log.warning("Lost connection while waiting for message: %s",
                             e)
            return 2
        except TypeError as e:
            self.log.error("Error while reading BGP message: %s", e)
            return 2
        except Exception as e:
            self.log.error("Error while reading BGP message: %s", e)
            raise

        if message.ID == exa_message.NOP.ID:
            return 1
        if message.ID == exa_message.Update.ID:
            if self.fsm.state != bgp_peer_worker.FSM.Established:
                raise Exception("Update received but not in Established state")
            # more below
        elif message.ID == exa_message.KeepAlive.ID:
            self.enqueue(bgp_peer_worker.KEEP_ALIVE_RECEIVED)
            self.log.debug("Received message: %s", message)
        else:
            self.log.warning("Received unexpected message: %s", message)

        if isinstance(message, exa_message.Update):
            if message.nlris:
                for nlri in message.nlris:
                    if nlri.action == exa_message.IN.ANNOUNCED:
                        action = engine.RouteEvent.ADVERTISE
                    elif nlri.action == exa_message.IN.WITHDRAWN:
                        action = engine.RouteEvent.WITHDRAW
                    else:
                        raise Exception("should not be reached (action:%s)",
                                        nlri.action)
                    self._process_received_route(action, nlri,
                                                 message.attributes)
        return 1

    def _process_received_route(self, action, nlri, attributes):
        self.log.info("Received route: %s, %s", nlri, attributes)

        route_entry = engine.RouteEntry(nlri, None, attributes)

        if action == exa_message.IN.ANNOUNCED:
            self._advertise_route(route_entry)
        elif action == exa_message.IN.WITHDRAWN:
            self._withdraw_route(route_entry)
        else:
            raise Exception("unsupported action ??? (%s)" % action)

        # TODO(tmmorin): move RTC code out-of the peer-specific code
        if (nlri.afi, nlri.safi) == (exa.AFI.ipv4, exa.SAFI.rtc):
            self.log.info("Received an RTC route")

            if nlri.rt is None:
                self.log.info("Received RTC is a wildcard")

            # the semantic of RTC routes does not distinguish between AFI/SAFIs
            # if our peer subscribed to a Route Target, it means that we needs
            # to send him all routes of any AFI/SAFI carrying this RouteTarget.
            for (afi, safi) in self._active_families:
                if (afi, safi) != (exa.AFI.ipv4, exa.SAFI.rtc):
                    if action == exa_message.IN.ANNOUNCED:
                        self._subscribe(afi, safi, nlri.rt)
                    elif action == exa_message.IN.WITHDRAWN:
                        self._unsubscribe(afi, safi, nlri.rt)
                    else:
                        raise Exception("unsupported action ??? (%s)" % action)

    def _send(self, data):
        # (error if state not the right one for sending updates)
        self.log.debug("Sending %d bytes on socket to peer %s",
                       len(data), self.peer_address)
        try:
            for _unused in self.protocol.connection.writer(data):
                pass
        except Exception:
            self.log.exception("Was not able to send data")

    def _keep_alive_message_data(self):
        return exa_message.KeepAlive().message()

    def _update_for_route_event(self, event):
        try:
            r = exa_message.Update([event.route_entry.nlri],
                                   event.route_entry.attributes)
            return b''.join(r.messages(self.protocol.negotiated))
        except Exception:
            self.log.exception("Exception while generating message for "
                               "route %s", r)
            return b''

    # Looking Glass ###############

    def get_lg_local_info(self, path_prefix):
        return {
            "peeringAddresses": {"peer_address": self.peer_address,
                                 "local_address": self.local_address},
            "as_info": {"local": cfg.CONF.BGP.my_as,
                        "peer": cfg.CONF.BGP.my_as},
            "rtc": {"active": self.rtc_active,
                    "enabled": cfg.CONF.BGP.enable_rtc},
            "active_families": [repr(f) for f in self._active_families],
        }
