#!/usr/bin/env python3
"""Offline tests for encs-switch-tui - no switch, no curses, no network.

The switch is a link-local device on one specific chassis, so the only way
to keep this tool honest between visits to the hardware is to fake the
switch. A FakeSwitch answers every wcd GET from a fixture and records every
POST, which is enough to check three things that used to need the box:

  * every view can fetch, render its columns and draw its detail panel
  * every write produces well-formed XML with the element names Cisco's
    switch-confd used
  * save_config/apply_config round-trip

What it CANNOT check is whether the firmware accepts those writes. Fixtures
were written from switch-confd's own templates, not captured from hardware,
so a table whose real reply is shaped differently will pass here and fail
there. Run it on the box before believing a new view works.

    python3 scripts/60-test-tui.py [-v]
"""
import importlib.util
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
TUI = os.path.join(HERE, "..", "payload", "opt", "encs-host", "encs-switch-tui")

VERBOSE = "-v" in sys.argv
FAILURES = []
CHECKS = [0]


def load_tui():
    """Import a file with no .py extension as a module."""
    loader = SourceFileLoader("encs_switch_tui", os.path.abspath(TUI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


t = load_tui()


# ============================================================== assertions
def check(cond, what):
    CHECKS[0] += 1
    if cond:
        if VERBOSE:
            print(f"  ok   {what}")
    else:
        FAILURES.append(what)
        print(f"  FAIL {what}")


def check_raises(fn, what):
    try:
        fn()
    except Exception:
        check(True, what)
        return
    check(False, what)


# ================================================================ fixtures
# One fragment per table, in the shape switch-confd's templates imply. Two
# ports only - the point is exercising the code paths, not the port count.
FIXTURES = {
    "Standard802_3List": """
      <Standard802_3List>
        <Entry><interfaceName>gi0</interfaceName><linkState>1</linkState>
          <adminState>1</adminState><mediaType>1</mediaType>
          <speedOper>1000</speedOper><duplexOperMode>2</duplexOperMode>
          <MACAddress>00:11:22:33:44:55</MACAddress>
          <LAGID>0</LAGID><LACPEnabled>0</LACPEnabled></Entry>
        <Entry><interfaceName>gi1</interfaceName><linkState>2</linkState>
          <adminState>2</adminState><mediaType>1</mediaType>
          <speedOper>0</speedOper><duplexOperMode>4</duplexOperMode>
          <MACAddress>00:11:22:33:44:56</MACAddress>
          <LAGID>1</LAGID><LACPEnabled>2</LACPEnabled></Entry>
        <Entry><interfaceName>te2</interfaceName><linkState>1</linkState>
          <adminState>1</adminState><mediaType>2</mediaType>
          <speedOper>10000</speedOper><duplexOperMode>2</duplexOperMode>
          <MACAddress>00:11:22:33:44:60</MACAddress>
          <LAGID>0</LAGID><LACPEnabled>0</LACPEnabled>
          <!-- Autoneg off is what a real backplane port reports, and it is
               what made save_config sweep te2 into 11-port-settings.xml.
               Keep it here so the "no te in any replay file" check has
               something to catch. -->
          <autoNegotiationAdminEnabled>2</autoNegotiationAdminEnabled></Entry>
      </Standard802_3List>""",
    "LAGList": """
      <LAGList>
        <LAGEntry><interfaceName>LAG1</interfaceName>
          <PortList><PortEntry><portName>gi1</portName>
            <membershipType>3</membershipType></PortEntry></PortList>
        </LAGEntry>
      </LAGList>""",
    "StatisticsList": """
      <StatisticsList>
        <InterfaceStatisticsEntry><interfaceName>gi0</interfaceName>
          <receivePacketByteCount>1000</receivePacketByteCount>
          <receiveUnicastPacketCount>10</receiveUnicastPacketCount>
          <transmitPacketByteCount>2000</transmitPacketByteCount>
          <transmitUnicastPacketCount>20</transmitUnicastPacketCount>
          <packetErrorCount>0</packetErrorCount></InterfaceStatisticsEntry>
        <InterfaceStatisticsEntry><interfaceName>te2</interfaceName>
          <receivePacketByteCount>0</receivePacketByteCount>
          <receiveUnicastPacketCount>0</receiveUnicastPacketCount>
          <transmitPacketByteCount>0</transmitPacketByteCount>
          <transmitUnicastPacketCount>0</transmitUnicastPacketCount>
          <packetErrorCount>0</packetErrorCount></InterfaceStatisticsEntry>
      </StatisticsList>""",
    "ForwardingTable": """
      <ForwardingTable>
        <Entry><VLANID>2363</VLANID><MACAddress>aa:bb:cc:dd:ee:ff</MACAddress>
          <interfaceName>te2</interfaceName><addressType>2</addressType></Entry>
      </ForwardingTable>""",
    "PoEPSEInterfaceList": """
      <PoEPSEInterfaceList>
        <Interface><interfaceName>gi0</interfaceName><adminEnable>1</adminEnable>
          <detectionStatus>3</detectionStatus><powerClassification>4</powerClassification>
          <outputPower>12000</outputPower><powerLimit>30000</powerLimit></Interface>
      </PoEPSEInterfaceList>""",
    "VLANInterfaceMembershipTable": """
      <VLANInterfaceMembershipTable>
        <Entry><VLANID>1</VLANID><VLANName></VLANName>
          <taggedPorts></taggedPorts><untaggedPorts>gi0-gi7</untaggedPorts></Entry>
        <Entry><VLANID>2363</VLANID><VLANName>mgmt</VLANName>
          <taggedPorts>te2</taggedPorts><untaggedPorts></untaggedPorts></Entry>
        <Entry><VLANID>100</VLANID><VLANName>lab</VLANName>
          <taggedPorts>gi0</taggedPorts><untaggedPorts></untaggedPorts></Entry>
      </VLANInterfaceMembershipTable>""",
    "VLANInterfaceISList": """
      <VLANInterfaceISList>
        <Entry><interfaceName>gi0</interfaceName>
          <switchportModeAdmin>10</switchportModeAdmin>
          <generalPVID>1</generalPVID><generalTaggedVLANs>100</generalTaggedVLANs>
          <generalUntaggedVLANs>1</generalUntaggedVLANs>
          <accessPVID>1</accessPVID><trunkNativeVID>1</trunkNativeVID>
          <trunkMemberVLANs></trunkMemberVLANs></Entry>
      </VLANInterfaceISList>""",
    "SpanningTreeGlobalParam": """
      <SpanningTreeGlobalParam><enabled>2</enabled>
        <STPOperationMode>2</STPOperationMode></SpanningTreeGlobalParam>""",
    "STP": """
      <STP>
        <GlobalSetting><BPDUHandlingMode>2</BPDUHandlingMode>
          <pathCostDefaultValueType>2</pathCostDefaultValueType>
          <loopbackGuardEnable>2</loopbackGuardEnable>
          <BridgeSetting><forwardDelay>15</forwardDelay><helloTime>2</helloTime>
            <maxAge>20</maxAge><bridgePriority>32768</bridgePriority>
          </BridgeSetting></GlobalSetting>
        <InterfaceList>
          <InterfaceEntry><interfaceName>gi0</interfaceName>
            <STPEnabled>1</STPEnabled><portState>5</portState><portRole>3</portRole>
            <pathCost>20000</pathCost><portPriority>128</portPriority>
            <portFastEnabled>2</portFastEnabled><BPDUGuardEnabled>2</BPDUGuardEnabled>
            <rootGuardEnabled>2</rootGuardEnabled></InterfaceEntry>
          <InterfaceEntry><interfaceName>gi1</interfaceName>
            <STPEnabled>2</STPEnabled><portState>1</portState><portRole>1</portRole>
            <pathCost>0</pathCost><portPriority>128</portPriority>
            <portFastEnabled>2</portFastEnabled><BPDUGuardEnabled>2</BPDUGuardEnabled>
            <rootGuardEnabled>2</rootGuardEnabled></InterfaceEntry>
          <InterfaceEntry><interfaceName>te2</interfaceName>
            <STPEnabled>1</STPEnabled><pathCost>2000</pathCost>
            <portPriority>128</portPriority></InterfaceEntry>
        </InterfaceList>
      </STP>""",
    "StormControlTable": """
      <StormControlTable>
        <Entry><interfaceName>gi0</interfaceName>
          <broadcastRateValue>100</broadcastRateValue>
          <broadcastUnitType>2</broadcastUnitType>
          <unicastRateValue>0</unicastRateValue><unicastUnitType>2</unicastUnitType>
          <multicastRateValue>500</multicastRateValue>
          <multicastUnitType>1</multicastUnitType></Entry>
        <Entry><interfaceName>te2</interfaceName>
          <broadcastRateValue>10</broadcastRateValue>
          <broadcastUnitType>2</broadcastUnitType></Entry>
      </StormControlTable>""",
    "SpanDestinationTable": """
      <SpanDestinationTable>
        <Entry><sessionId>1</sessionId><ifIndex>3</ifIndex>
          <isReflector>1</isReflector><portType>1</portType>
          <remoteVlanId>0</remoteVlanId></Entry>
      </SpanDestinationTable>""",
    "SpanSourceTable": """
      <SpanSourceTable>
        <Entry><sessionId>1</sessionId><sourceType>1</sourceType>
          <sourceIdx>1</sourceIdx><sourceDirection>3</sourceDirection></Entry>
      </SpanSourceTable>""",
    "ForwardingStaticTable": """
      <ForwardingStaticTable>
        <Entry><VLANID>100</VLANID><MACAddress>00:de:ad:be:ef:00</MACAddress>
          <interfaceName>gi0</interfaceName>
          <addressStatus>3</addressStatus></Entry>
      </ForwardingStaticTable>""",
    "ForwardingGlobalSetting": """
      <ForwardingGlobalSetting><agingInterval>300</agingInterval>
      </ForwardingGlobalSetting>""",
    "LLDPGlobalSetting": """
      <LLDPGlobalSetting><LLDPEnabled>1</LLDPEnabled>
        <updateInterval>30</updateInterval></LLDPGlobalSetting>""",
    "LLDPInterfaceList": """
      <LLDPInterfaceList>
        <InterfaceEntry><interfaceName>gi0</interfaceName>
          <portState>3</portState></InterfaceEntry>
        <InterfaceEntry><interfaceName>te2</interfaceName>
          <portState>1</portState></InterfaceEntry>
      </LLDPInterfaceList>""",
    "CDPInterfaceList": """
      <CDPInterfaceList>
        <Entry><interfaceName>gi0</interfaceName><enbl>1</enbl></Entry>
        <Entry><interfaceName>te2</interfaceName><enbl>2</enbl></Entry>
      </CDPInterfaceList>""",
    "LACPGlobalSetting": """
      <LACPGlobalSetting><LACPSystemPriority>1</LACPSystemPriority>
        <LAGLoadBalance>1</LAGLoadBalance></LACPGlobalSetting>""",
    "LACPPortList": """
      <LACPPortList>
        <Entry><interfaceName>gi0</interfaceName>
          <actorPortPriority>1</actorPortPriority>
          <actorPortAdminTimeout>1</actorPortAdminTimeout>
          <actorAdminKey>0</actorAdminKey></Entry>
        <Entry><interfaceName>te2</interfaceName>
          <actorPortPriority>9</actorPortPriority>
          <actorPortAdminTimeout>2</actorPortAdminTimeout></Entry>
      </LACPPortList>""",
    "PrivateVLANTable": """
      <PrivateVLANTable>
        <Entry><VLANID>200</VLANID><privateVLANType>1</privateVLANType></Entry>
      </PrivateVLANTable>""",
    "PrivateVLANAssociationTable": """
      <PrivateVLANAssociationTable>
        <Entry><primaryVLANID>200</primaryVLANID>
          <communityVLANsList>201</communityVLANsList></Entry>
      </PrivateVLANAssociationTable>""",
    "PrivateVLANHostPortTable": """
      <PrivateVLANHostPortTable>
        <Entry><interfaceName>gi1</interfaceName><primaryVLAN>200</primaryVLAN>
          <secondaryVLAN>201</secondaryVLAN></Entry>
      </PrivateVLANHostPortTable>""",
    "PrivateVLANPromiscuousPortTable": """
      <PrivateVLANPromiscuousPortTable>
        <Entry><interfaceName>gi0</interfaceName><primaryVLAN>200</primaryVLAN>
          <secondaryVLANList>201</secondaryVLANList></Entry>
      </PrivateVLANPromiscuousPortTable>""",
    "ACLList": """
      <ACLList>
        <ACLEntry><ACLName>guard</ACLName><ACLType>1</ACLType></ACLEntry>
      </ACLList>""",
    "ACEList": """
      <ACEList>
        <Entry><ACLName>guard</ACLName><ruleType>1</ruleType>
          <ruleAction>2</ruleAction><rulePriority>10</rulePriority>
          <sourceMACAddress>00:11:22:33:44:55</sourceMACAddress></Entry>
      </ACEList>""",
    "ACLBindingList": """
      <ACLBindingList>
        <ACLBindingEntry><interfaceName>gi0</interfaceName>
          <ACLIPv4MACName>guard</ACLIPv4MACName>
          <defaultRuleAction>2</defaultRuleAction></ACLBindingEntry>
        <ACLBindingEntry><interfaceName>te2</interfaceName>
          <ACLIPv4MACName>guard</ACLIPv4MACName>
          <defaultRuleAction>2</defaultRuleAction></ACLBindingEntry>
      </ACLBindingList>""",
    "QoSSettingGlobalParam": """
      <QoSSettingGlobalParam><QoSMode>1</QoSMode>
        <basicTrustMode>1</basicTrustMode></QoSSettingGlobalParam>""",
    "CoSSetting": """
      <CoSSetting><InterfaceList>
        <Interface><interfaceName>gi0</interfaceName><CoS>0</CoS></Interface>
        <Interface><interfaceName>te2</interfaceName><CoS>4</CoS></Interface>
      </InterfaceList></CoSSetting>""",
    "CoSToQueueMappingList": """
      <CoSToQueueMappingList>
        <CoSMappingEntry><CoS>0</CoS><queueNumber>1</queueNumber></CoSMappingEntry>
      </CoSToQueueMappingList>""",
    "AggregatePolicerList": """
      <AggregatePolicerList>
        <Entry><policerName>p1</policerName><CIR>1000</CIR><CBS>3000</CBS>
          <action>1</action></Entry>
      </AggregatePolicerList>""",
    "Standard_802_1xGlobalSetting": """
      <Standard_802_1xGlobalSetting>
        <enabled>2</enabled></Standard_802_1xGlobalSetting>""",
    "Standard_802_1xInterfaceList": """
      <Standard_802_1xInterfaceList>
        <Entry><interfaceName>gi0</interfaceName><portControl>1</portControl>
          <hostMode>2</hostMode></Entry>
      </Standard_802_1xInterfaceList>""",
    "RadiusServerList": """
      <RadiusServerList>
        <RadiusServer><IPAddress>10.0.0.5</IPAddress><authPortNo>1812</authPortNo>
          <accountPortNo>1813</accountPortNo><serverPriority>1</serverPriority>
          <timeoutForReply>3</timeoutForReply>
          <keyString>s3cret</keyString></RadiusServer>
      </RadiusServerList>""",
    "RadiusDefaultParam": """
      <RadiusDefaultParam><timeoutForReply>3</timeoutForReply>
        <numberOfRetries>3</numberOfRetries></RadiusDefaultParam>""",
    "MulticastGlobalSetting": """
      <MulticastGlobalSetting><IGMPSnoopEnbl>2</IGMPSnoopEnbl>
        <multicastFilterEnbl>2</multicastFilterEnbl></MulticastGlobalSetting>""",
    "IGMPMLDSnoopVLANList": """
      <IGMPMLDSnoopVLANList>
        <Entry><VLANID>100</VLANID><snoopEnbl>2</snoopEnbl>
          <querierEnbl>2</querierEnbl><immediateLeave>2</immediateLeave></Entry>
      </IGMPMLDSnoopVLANList>""",
    "IGMPMLDSnoopRouterPortList": """
      <IGMPMLDSnoopRouterPortList>
        <Entry><VLANID>100</VLANID><staticPortList>gi0</staticPortList></Entry>
      </IGMPMLDSnoopRouterPortList>""",
    "IGMPMLDSnoopGroupList": "<IGMPMLDSnoopGroupList></IGMPMLDSnoopGroupList>",
    "ARPList": """
      <ARPList>
        <ARPEntry><interfaceName>gi0</interfaceName><IPAddress>10.0.0.1</IPAddress>
          <physicalAddress>00:11:22:33:44:99</physicalAddress></ARPEntry>
      </ARPList>""",
    "ARPGlobalSetting": "<ARPGlobalSetting><timeout>60000</timeout></ARPGlobalSetting>",
    # Routing off, which is the default on an L2 switch.
    "IPv4GlobalSetting": """
      <IPv4GlobalSetting><unicastRoutingEnable>2</unicastRoutingEnable>
      </IPv4GlobalSetting>""",
    "IPv4InterfaceList": """
      <IPv4InterfaceList>
        <Entry><interfaceName>vlan2363</interfaceName>
          <IPAddress>169.254.1.0</IPAddress></Entry>
      </IPv4InterfaceList>""",
    "IPv4RouteList": """
      <IPv4RouteList>
        <Entry><destinationIPv4Address>0.0.0.0</destinationIPv4Address>
          <destinationPrefixLength>0</destinationPrefixLength>
          <nextHopIPv4Address>169.254.1.1</nextHopIPv4Address>
          <routeType>2</routeType><metric>1</metric></Entry>
      </IPv4RouteList>""",
    "IPv4GatewayList": """
      <IPv4GatewayList>
        <GWEntry><IPAddr>169.254.1.1</IPAddr><fwdStatus>1</fwdStatus></GWEntry>
      </IPv4GatewayList>""",
    "MSTPGlobalSetting": """
      <MSTPGlobalSetting><revision>0</revision></MSTPGlobalSetting>""",
    "MSTPInstanceList": "<MSTPInstanceList></MSTPInstanceList>",
    "MSTPVLANList": "<MSTPVLANList></MSTPVLANList>",
}

OK_BODY = ("<?xml version='1.0' encoding='utf-8'?><DeviceConfiguration>"
           "<ActionStatus><statusCode>0</statusCode>"
           "<statusString>OK</statusString></ActionStatus>"
           "</DeviceConfiguration>")


class FakeSwitch(t.Switch):
    """A Switch that never opens a socket."""

    def __init__(self, missing=()):
        super().__init__(ip="203.0.113.1")
        self.sid = "fake"
        self.posts = []
        # Tables this "firmware" does not implement, to prove a view
        # survives one going away.
        self.missing = set(missing)

    def login(self):
        """Never touch the network.

        _request is overridden below, but login() and logout() call urlopen
        directly. Anything driving this double through the normal
        login/work/logout flow would otherwise make a real connection to
        203.0.113.1 and block until it timed out.
        """
        self.sid = "fake"
        return self.sid

    def logout(self):
        self.sid = None

    def _request(self, url, data=None, retry=True):
        if data is not None:
            body = data.decode("utf8")
            # Every write must be parseable XML - a malformed body is the
            # failure mode that costs an afternoon on the real switch.
            root = ET.fromstring(body)
            # An empty-text element in a "set" is what hung the real switch
            # twice on 2026-08-11 and cost two power cycles. Clearing a
            # field is action="restore" with a self-closing element, never
            # action="set" with "". Refuse it here so no test, and no code
            # path a test exercises, can produce one again.
            for tbl in root:
                if tbl.tag == "version" or tbl.get("action") == "restore":
                    continue
                for el_ in tbl.iter():
                    if len(el_) == 0 and el_.text is not None \
                            and el_.text.strip() == "" and el_ is not tbl:
                        raise AssertionError(
                            f"empty-text <{el_.tag}></{el_.tag}> in an "
                            f'action="{tbl.get("action")}" write - this '
                            f"hangs the switch; use the restore path")
            self.posts.append(body)
            return OK_BODY
        want = [n.strip("{}") for n in url.split("?", 1)[1].split("}{")]
        want = [n.strip("{}") for n in want]
        frags = []
        for name in want:
            key = name.split("&")[0]
            if key in self.missing:
                raise t.SwitchError(f"no such table {key}")
            frags.append(FIXTURES.get(key, f"<{key}></{key}>"))
        return ("<?xml version='1.0' encoding='utf-8'?><DeviceConfiguration>"
                + "".join(frags) + "</DeviceConfiguration>")


def headless_ui(sw):
    """A UI with no curses attached.

    __init__ talks to curses, so bypass it and set only what the logic
    below reads. This is enough to exercise every action and renderer.
    """
    ui = t.UI.__new__(t.UI)
    ui.s, ui.sw = None, sw
    ui.view, ui.sel, ui.top = "ports", 0, 0
    ui.msg, ui.msg_bad = "", False
    ui.detail, ui.help_scroll = True, 0
    ui.data, ui.last_refresh = {}, 0
    ui.connected, ui.update_tag = True, None
    ui.details = {k: f(ui) for k, f in t.DETAIL_FACTORIES.items()}
    ui.answers = []
    ui.prompt = lambda q, maxlen=12: (ui.answers.pop(0)
                                      if ui.answers else None)
    return ui


# =================================================================== tests
def test_xml_builders():
    print("XML builders")
    d = t.entry_doc("VLANList", {"VLANID": "100"}, tag="VLAN")
    root = ET.fromstring(d)
    check(root.tag == "DeviceConfiguration", "envelope is DeviceConfiguration")
    check(root.findtext("version") == "1.0", "carries version 1.0")
    vl = root.find("VLANList")
    check(vl is not None and vl.get("action") == "set", "action attribute set")
    check(vl.findtext("VLAN/VLANID") == "100", "value lands in the right path")

    # Escaping: the reason xesc exists.
    d = t.entry_doc("ACLList", {"ACLName": 'a&b<c>"d"'}, tag="ACLEntry")
    check(ET.fromstring(d).findtext("ACLList/ACLEntry/ACLName") == 'a&b<c>"d"',
          "ampersands and angle brackets survive a round trip")

    # None and empty string are BOTH dropped. An empty-text element in a
    # "set" hung the real switch; clearing goes through the restore path.
    body = t.fields({"a": None, "b": "", "c": "1"})
    check("<a>" not in body, "None omits the element")
    check("<b>" not in body, "an empty string omits the element too")
    check("<c>1</c>" in body, "a real value is kept")

    # Nesting, as STP's BridgeSetting needs.
    d = t.doc("STP", t.el("GlobalSetting", t.el("BridgeSetting",
                                                t.fields({"maxAge": "20"}))))
    check(ET.fromstring(d).findtext("STP/GlobalSetting/BridgeSetting/maxAge")
          == "20", "nested element paths build correctly")


def test_span_index():
    print("SPAN index arithmetic")
    check(t.span_index("gi0") == "1", "gi0 is index 1")
    check(t.span_index("gi7") == "8", "gi7 is index 8")
    check(t.span_index("te1") == "9", "te1 is index 9")
    check(t.span_index("te4") == "12", "te4 is index 12")
    for n in ("gi0", "gi7", "te1", "te4"):
        check(t.span_ifname(t.span_index(n)) == n, f"{n} round-trips")
    check(t.span_vlan_index(1) == "100000", "VLAN 1 is the offset itself")
    check(t.span_ifname("100099") == "VLAN 100", "VLAN index decodes")
    check_raises(lambda: t.span_index("LAG1"), "a LAG cannot be a SPAN port")


def test_fetches_and_render():
    print("every view fetches, renders and details")
    sw = FakeSwitch()
    ui = headless_ui(sw)
    for name, spec in sorted(t.SPECS.items()):
        try:
            data = spec["fetch"](sw)
        except Exception as e:
            check(False, f"{name}: fetch raised {e!r}")
            continue
        check(isinstance(data.get("rows"), list), f"{name}: fetch returns rows")
        ui.view, ui.data[name] = name, data
        rows = data["rows"]
        check(bool(rows), f"{name}: fixture produced at least one row")

        # Columns must survive both a real row and a row missing everything.
        for r in list(rows) + [{}]:
            for head, _, fn in spec["cols"]:
                try:
                    fn(r)
                except Exception as e:
                    check(False, f"{name}.{head}: column raised {e!r}")
        check(True, f"{name}: all columns render (incl. an empty row)")

        if spec.get("summary"):
            for x in (data.get("extra", {}), {}):
                try:
                    lines = spec["summary"](x)
                    check(all(isinstance(s, str) for s in lines),
                          f"{name}: summary returns strings")
                except Exception as e:
                    check(False, f"{name}: summary raised {e!r}")

        for r in list(rows) + [{}]:
            ui.sel = 0
            try:
                lines = ui.d_spec_detail(r)
                check(all(isinstance(s, str) for s in lines),
                      f"{name}: detail returns strings")
            except Exception as e:
                check(False, f"{name}: detail raised {e!r}")


def test_missing_tables():
    print("a view survives a table this firmware does not have")
    # Every side table gone; the primary read still works.
    side = ["SpanSourceTable", "CDPInterfaceList", "LACPGlobalSetting",
            "PrivateVLANAssociationTable", "PrivateVLANHostPortTable",
            "PrivateVLANPromiscuousPortTable", "ACEList", "ACLBindingList",
            "QoSSettingGlobalParam", "CoSToQueueMappingList",
            "AggregatePolicerList", "Standard_802_1xGlobalSetting",
            "RadiusDefaultParam", "MulticastGlobalSetting",
            "IGMPMLDSnoopRouterPortList", "IPv4GatewayList", "ARPList",
            "ARPGlobalSetting", "IPv4InterfaceList", "ForwardingGlobalSetting",
            "LLDPGlobalSetting", "SpanningTreeGlobalParam"]
    sw = FakeSwitch(missing=side)
    for name, spec in sorted(t.SPECS.items()):
        try:
            data = spec["fetch"](sw)
            if spec.get("summary"):
                spec["summary"](data.get("extra", {}))
            check(isinstance(data.get("rows"), list),
                  f"{name}: degrades to rows-only")
        except Exception as e:
            check(False, f"{name}: raised {e!r} with side tables missing")


def test_writes():
    print("writes produce the element names switch-confd used")
    sw = FakeSwitch()

    def last(fn):
        sw.posts.clear()
        fn()
        return ET.fromstring(sw.posts[-1])

    r = last(lambda: sw.set_port_admin("gi0", False))
    check(r.findtext("Standard802_3List/Entry/adminState") == "2",
          "port shut writes adminState 2")

    r = last(lambda: sw.set_channel_group("gi3", 1, "2"))
    check(r.findtext("Standard802_3List/Entry/LAGID") == "1"
          and r.findtext("Standard802_3List/Entry/LACPEnabled") == "2",
          "channel-group targets the member port")

    r = last(lambda: sw.vlan("100", True, "lab"))
    check(r.findtext("VLANList/VLAN/VLANName") == "lab", "VLAN name is written")

    r = last(lambda: sw.set_vlan_interface("gi0", mode="11", access_pvid="100"))
    e = r.find("VLANInterfaceISList/Entry")
    check(e.findtext("switchportModeAdmin") == "11"
          and e.findtext("accessPVID") == "100"
          and e.find("trunkNativeVID") is None,
          "access mode writes accessPVID and nothing trunk-shaped")

    r = last(lambda: sw.set_stp_bridge({"maxAge": "20"}))
    check(r.findtext("STP/GlobalSetting/BridgeSetting/maxAge") == "20",
          "STP timers nest under BridgeSetting")

    r = last(lambda: sw.set_stp_port("gi0", {"pathCost": "100"}))
    check(r.findtext("STP/InterfaceList/InterfaceEntry/pathCost") == "100",
          "per-port STP nests under InterfaceList")

    r = last(lambda: sw.set_stp_port("gi0", {"pointToPointAdminStatusMode": "1"},
                                     table="RSTP"))
    check(r.find("RSTP/InterfaceList/InterfaceEntry") is not None,
          "link type goes to the RSTP table, not STP")

    r = last(lambda: sw.set_storm("gi0", "multicast", "50", t.STORM_LEVEL))
    e = r.find("StormControlTable/Entry")
    check(e.findtext("multicastRateValue") == "50"
          and e.findtext("multicastType") == "4",
          "multicast storm control carries multicastType 4")

    r = last(lambda: sw.add_span_source(1, ifname="gi0"))
    e = r.find("SpanSourceTable/Entry")
    check(r.find("SpanSourceTable").get("action") == "add"
          and e.findtext("sourceIdx") == "1" and e.findtext("sourceType") == "1",
          "SPAN source adds by index, not name")

    r = last(lambda: sw.del_static_mac("100", "00:de:ad:be:ef:00"))
    tbl = r.find("ForwardingStaticTable")
    check(tbl.get("action") == "delete"
          and tbl.find("Entry/addressStatus") is not None,
          "static MAC delete carries the empty addressStatus element")

    r = last(lambda: sw.set_arp("gi0", "10.0.0.1", "00:11:22:33:44:99"))
    check(r.findtext("ARPList/ARPEntry/physicalAddress") == "00:11:22:33:44:99",
          "ARP uses physicalAddress, not MACAddress")

    r = last(lambda: sw.set_gateway("10.0.0.254"))
    e = r.find("IPv4GatewayList/GWEntry")
    check(e.findtext("IPAddr") == "10.0.0.254" and e.findtext("owner") == "1",
          "gateway uses IPAddr and the required constants")

    r = last(lambda: sw.set_route("10.0.0.0", "8", "10.0.0.1", "1"))
    check(r.find("IPv4RouteList").get("action") == "add"
          and r.findtext("IPv4RouteList/Entry/destinationPrefixLength") == "8",
          "routes are added with a prefix length")

    r = last(lambda: sw.set_policer("p1", "1000", "3000"))
    e = r.find("AggregatePolicerList/Entry")
    check(e.findtext("policerName") == "p1" and e.findtext("PIR") == "0",
          "policer sends the required PIR/PBS zeros")

    r = last(lambda: sw.set_class_map("c1"))
    check(r.findtext("ClassMapList/ClassMapEntry/className") == "c1",
          "class map uses className in a ClassMapEntry")

    r = last(lambda: sw.set_radius_server("10.0.0.5", {"keyString": "s3cret"}))
    check(r.findtext("RadiusServerList/RadiusServer/keyString") == "s3cret",
          "RADIUS server is a RadiusServer element")

    r = last(lambda: sw.clear_counters("gi0"))
    check(r.findtext("PortStatisticsClear/clearPorts") == "gi0",
          "counter clear takes the port directly, with no Entry")

    r = last(lambda: sw.set_port_cos("gi0", "5"))
    check(r.findtext("CoSSetting/InterfaceList/Interface/CoS") == "5",
          "port CoS nests under InterfaceList/Interface")

    r = last(lambda: sw.set_port_shaper("gi0", "1000", "3000"))
    e = r.find("QoSBandwidthList/Entry")
    check(e.findtext("interfaceName") == "gi0"
          and e.findtext("shaperEnable") == "1",
          "the shaper is indexed by interface, not by queue")

    r = last(lambda: sw.set_dscp_queue("46", "8"))
    check(r.findtext("DSCPMapping/DSCPList/DSCPEntry/queueNumber") == "8",
          "DSCP->queue is nested two levels deep")

    r = last(lambda: sw.set_dscp_remark("46", "0"))
    check(r.findtext("DSCPRemark/DSCPRemarkList/DSCPRemarkEntry/DSCPOut")
          == "0", "DSCP remark uses its own doubly-nested list")

    r = last(lambda: sw.flush_macs("gi0"))
    check(r.find("ForwardingTable").get("action") == "delete"
          and r.findtext("ForwardingTable/Entry/interfaceName") == "gi0",
          "per-port MAC flush deletes by interface")

    r = last(lambda: sw.flush_macs())
    tbl = r.find("ForwardingTable")
    check(tbl is not None and tbl.get("action") == "deleteAll",
          "a full MAC flush uses deleteAll")


def test_ace_params():
    print("MAC ACE address blocks")
    any_ = t.mac_ace_param("source", "any")
    check("<sourceAddressType>4</sourceAddressType>" in any_,
          "'any' is address type 4 and carries no address")
    check("sourceMACAddress" not in any_, "'any' writes no address element")
    one = t.mac_ace_param("dest", "00:11:22:33:44:55")
    check("<destAddressType>1</destAddressType>" in one, "a MAC is type 1")
    check("<destMaskBits>48</destMaskBits>" in one,
          "a bare MAC defaults to a 48-bit mask")
    masked = t.mac_ace_param("source", "00:11:22:33:44:55/24")
    check("<sourceMaskBits>24</sourceMaskBits>" in masked,
          "an explicit mask width is honoured")


def test_actions():
    print("actions build the write they promise")
    sw = FakeSwitch()
    ui = headless_ui(sw)

    def run(view, action, answers):
        ui.view = view
        ui.data[view] = t.SPECS[view]["fetch"](sw) if view in t.SPECS \
            else ui.data.get(view)
        ui.sel = 0
        ui.answers = list(answers)
        sw.posts.clear()
        getattr(ui, action)()
        return sw.posts

    posts = run("stp", "act_stp_global", ["e", "y"])
    check(len(posts) == 1
          and ET.fromstring(posts[0]).findtext(
              "SpanningTreeGlobalParam/enabled") == "1",
          "enabling STP writes enabled=1")

    # 802.1D timer relationship: 2*(fwd-1) >= maxAge.
    posts = run("stp", "act_stp_global", ["t", "2", "4", "20"])
    check(not posts, "an invalid STP timer set is refused before writing")
    check("invalid" in ui.msg, "and says why")

    posts = run("stp", "act_stp_global", ["t", "2", "15", "20"])
    check(len(posts) == 1, "a valid STP timer set is written")

    posts = run("stp", "act_stp_global", ["p", "5000"])
    check(ET.fromstring(posts[0]).findtext(
        "STP/GlobalSetting/BridgeSetting/bridgePriority") == "4096",
        "bridge priority is rounded down to a multiple of 4096")

    posts = run("storm", "act_storm", ["b", "p", "50"])
    e = ET.fromstring(posts[0]).find("StormControlTable/Entry")
    check(e.findtext("broadcastRateValue") == "50"
          and e.findtext("broadcastUnitType") == t.STORM_LEVEL,
          "storm control writes rate and unit together")

    posts = run("staticmac", "act_staticmac_new",
                ["100", "00:de:ad:be:ef:01", "gi0"])
    check(len(posts) == 1, "a static MAC is written")
    posts = run("staticmac", "act_staticmac_new", ["100", "nonsense", "gi0"])
    check(not posts, "a malformed MAC is refused")

    posts = run("acl", "act_acl_rule", ["p", "10", "any", "any"])
    check(len(posts) == 1
          and "<sourceAddressType>4</sourceAddressType>" in posts[0],
          "an ACL rule with any/any builds address type 4")

    posts = run("l3", "act_l3_route_new", ["10.0.0.0", "8", "10.0.0.1", "1"])
    check(len(posts) == 1, "a static route is written")

    # Enabling 802.1X with no RADIUS server would fail every port closed.
    ui.data["dot1x"] = {"rows": [], "extra": {"global": {}, "servers": []}}
    ui.view, ui.answers = "dot1x", ["y"]
    sw.posts.clear()
    ui.act_dot1x_global()
    check(not sw.posts, "802.1X cannot be enabled with no RADIUS server")
    check("REFUSED" in ui.msg, "and says why")
    ui.view, ui.answers = "dot1x", ["n"]
    sw.posts.clear()
    ui.act_dot1x_global()
    check(len(sw.posts) == 1,
          "...but disabling 802.1X is always allowed - that is the way out")

    # STP's convergence warning must survive the write that follows it.
    posts = run("stp", "act_stp_global", ["e", "y"])
    check("30s" in ui.msg,
          "enabling STP leaves the convergence warning on screen")

    # A delete must not carry an empty value element.
    ui.data["pvlan"] = t.SPECS["pvlan"]["fetch"](sw)
    ui.view, ui.sel, ui.answers = "pvlan", 0, ["yes"]
    sw.posts.clear()
    ui.act_pvlan_delete()
    check(sw.posts and "<privateVLANType>" not in sw.posts[0],
          "a private-VLAN delete omits the type rather than sending it empty")

    # Cancelling at any prompt must write nothing.
    for view, action, answers in [
            ("stp", "act_stp_global", []),
            ("storm", "act_storm", ["b"]),
            ("staticmac", "act_staticmac_new", ["100"]),
            ("l3", "act_l3_route_new", ["10.0.0.0", "8"]),
            ("acl", "act_acl_new", [])]:
        check(not run(view, action, answers),
              f"{action}: cancelling writes nothing")


def test_vlan_lists():
    print("VLAN list merging")
    check(t.vlan_list_parse("1,100-102") == {1, 100, 101, 102},
          "ranges expand")
    check(t.vlan_list_format({1, 100, 101, 102}) == "1,100-102",
          "runs collapse back into ranges")
    check(t.vlan_list_add("100", 200) == "100,200",
          "adding keeps the VLAN already there")
    check(t.vlan_list_add("", 200) == "200", "adding to an empty list works")
    check(t.vlan_list_add("100,200", 200) == "100,200",
          "adding one already present changes nothing")
    check(t.vlan_list_remove("100,200,300", 200) == "100,300",
          "removing keeps the others")
    check(t.vlan_list_remove("100", 100) == "", "removing the last empties it")
    check(t.vlan_list_add("1-4093", 4094) == "1-4094",
          "a huge list stays collapsed rather than expanding to 4094 items")


def test_vlan_membership_merges():
    """The regression that matters most.

    Every per-port VLAN write is action="set" on the whole list, so sending
    a single VLAN id replaces it. The first version of this editor did
    exactly that: adding a port to a second VLAN silently removed it from
    the first.
    """
    print("adding a port to a VLAN does not remove it from others")
    sw = FakeSwitch()
    ui = headless_ui(sw)
    ui.view = "vlans"
    ui.data["vlans"] = sw.vlans()
    # gi0 is already tagged in VLAN 100 per the fixture. Add it to VLAN 1
    # as tagged and both must survive.
    ui.sel = [i for i, r in enumerate(ui.data["vlans"])
              if r["VLANID"] == "1"][0]
    ui.answers = ["a", "gi0", "g", "t"]
    sw.posts.clear()
    ui.act_vlan_ports()
    check(len(sw.posts) == 1, "one write is posted")
    got = ET.fromstring(sw.posts[0]).findtext(
        "VLANInterfaceISList/Entry/generalTaggedVLANs")
    check(got == "1,100",
          f"tagged list merges to '1,100', not '{got}'")

    # Trunk membership must merge the same way.
    ui.answers = ["a", "gi0", "t", "n"]
    sw.posts.clear()
    ui.act_vlan_ports()
    check(len(sw.posts) == 1, "trunk write is posted")
    e = ET.fromstring(sw.posts[0]).find("VLANInterfaceISList/Entry")
    check(e.findtext("trunkMemberVLANs") == "1",
          "trunk members merge into the existing list")
    check(e.find("trunkNativeVID") is None,
          "a non-native trunk add does not set a native VLAN")

    # Removal must take the VLAN out and leave the rest.
    ui.sel = [i for i, r in enumerate(ui.data["vlans"])
              if r["VLANID"] == "100"][0]
    ui.answers = ["r", "gi0"]
    sw.posts.clear()
    ui.act_vlan_ports()
    check(len(sw.posts) == 1, "removal posts one write")
    root = ET.fromstring(sw.posts[0])
    tbl = root.find("VLANInterfaceISList")
    e = tbl.find("Entry")
    # VLAN 100 was gi0's only tagged VLAN, so the list becomes empty. That
    # MUST go out as action="restore" with a self-closing element - the
    # action="set" + "" form hangs the switch until AC is pulled.
    check(tbl.get("action") == "restore",
          "clearing the last VLAN uses action=restore")
    tag = e.find("generalTaggedVLANs")
    check(tag is not None and not (tag.text or "").strip(),
          "...with a self-closing generalTaggedVLANs")
    check(e.find("generalUntaggedVLANs") is None,
          "...and does not touch the untagged list VLAN 100 was never in")


def test_no_empty_set_elements():
    """The regression that cost two power cycles.

    Removing a port from its only VLAN used to send
    action="set" <generalTaggedVLANs></generalTaggedVLANs>, which hung the
    switch. FakeSwitch now rejects any empty-text element in a non-restore
    write, so this walks the removal paths that produce one.
    """
    print("no write ever sets an empty value")
    sw = FakeSwitch()
    ui = headless_ui(sw)
    ui.view = "vlans"
    ui.data["vlans"] = sw.vlans()

    # gi0's only tagged VLAN is 100; removing it empties the list.
    ui.sel = [i for i, r in enumerate(ui.data["vlans"])
              if r["VLANID"] == "100"][0]
    for answers in (["r", "gi0"],):
        ui.answers = list(answers)
        sw.posts.clear()
        try:
            ui.act_vlan_ports()
            check(True, f"removal via {answers} produced no empty set")
        except AssertionError as e:
            check(False, f"removal via {answers}: {e}")

    # And the client call directly.
    try:
        sw.clear_vlan_interface("gi0", "generalTaggedVLANs")
        root = ET.fromstring(sw.posts[-1])
        check(root.find("VLANInterfaceISList").get("action") == "restore",
              "clear_vlan_interface uses action=restore")
        check("<generalTaggedVLANs/>" in sw.posts[-1],
              "...and a self-closing element")
    except AssertionError as e:
        check(False, f"clear_vlan_interface: {e}")
    check_raises(lambda: sw.clear_vlan_interface("gi0"),
                 "clearing nothing is refused")


def test_mirror_guard_without_ports_view():
    """A guard that only works if you visited another view first is not a
    guard. mgmt_port() reads portmacs, which only the Ports view loaded."""
    print("the mirror destination guard works from a cold start")
    sw = FakeSwitch()
    ui = headless_ui(sw)
    ui.view = "mirror"
    ui.data["mirror"] = t.SPECS["mirror"]["fetch"](sw)
    check("portmacs" not in ui.data, "portmacs is not loaded in this view")

    # local_nics() reads /sys/class/net, which does not exist off-Linux.
    # Fake it so the MAC cross-reference is actually exercised rather than
    # silently returning "no local NICs" and passing for the wrong reason.
    # The fixture learns aa:bb:cc:dd:ee:ff on te2 in the management VLAN.
    real = t.local_nics
    t.local_nics = lambda: {"aa:bb:cc:dd:ee:ff": "enp8s0f1np1"}
    try:
        check(ui.mgmt_port() == "te2",
              "mgmt_port resolves without the Ports view having been opened")
        check(ui.data.get("portmacs"),
              "...by fetching the MAC table on demand")
        ui.answers = ["1", "te2"]
        sw.posts.clear()
        ui.act_mirror_dest()
        check(not sw.posts, "mirroring to the management port writes nothing")
    finally:
        t.local_nics = real


def test_guards():
    print("refusals")
    sw = FakeSwitch()
    ui = headless_ui(sw)
    ui.view = "ports"
    ui.data["ports"] = sw.port_snapshot()["ports"]
    ui.data["portmacs"] = sw.port_snapshot()["macs"]

    check(ui.may_shut("gi0") is True, "shutting a front port is allowed")
    check(ui.may_shut("te2") is False, "shutting a backplane port is refused")
    check("te2" in ui.msg, "the refusal names the port")

    ui.answers = ["gi0"]
    check(ui.ask_port() == "gi0", "a valid port is accepted")
    ui.answers = ["te1"]
    check(ui.ask_port() is None, "a te port is refused by default")
    ui.answers = ["te1"]
    check(ui.ask_port(allow_te=True) == "te1", "unless explicitly allowed")
    ui.answers = ["gi99"]
    check(ui.ask_port() is None, "a port that does not exist is refused")

    ui.answers = ["5"]
    check(ui.ask_int("x", 1, 10) == "5", "an in-range integer passes")
    ui.answers = ["50"]
    check(ui.ask_int("x", 1, 10) is None, "an out-of-range integer is refused")

    # The management VLAN must not be editable from the membership editor.
    ui.view = "vlans"
    ui.data["vlans"] = sw.vlans()
    ui.sel = [i for i, r in enumerate(ui.data["vlans"])
              if r["VLANID"] == "2363"][0]
    sw.posts.clear()
    ui.answers = ["gi0", "a"]
    ui.act_vlan_ports()
    check(not sw.posts, "the management VLAN cannot be edited")


def test_config_roundtrip():
    print("config save and replay")
    sw = FakeSwitch()
    with tempfile.TemporaryDirectory() as d:
        written = t.save_config(sw, d)
        names = sorted(os.path.basename(f) for f in written)
        for want in ("10-ports.xml", "15-lag.xml", "20-vlans.xml",
                     "25-vlan-ports.xml", "30-poe.xml"):
            check(want in names, f"save_config writes {want}")
        for f in written:
            body = open(f).read()
            try:
                ET.fromstring(body)
                check(True, f"{os.path.basename(f)} is well-formed XML")
            except ET.ParseError as e:
                check(False, f"{os.path.basename(f)} is malformed: {e}")

        # The system VLANs must not be replayed.
        vl = open(os.path.join(d, "20-vlans.xml")).read()
        check("<VLANID>100</VLANID>" in vl, "a user VLAN is saved")
        check("<VLANID>2363</VLANID>" not in vl,
              "the management VLAN is not saved")
        check("<VLANID>1</VLANID>" not in vl, "the default VLAN is not saved")

        # te ports must not be replayed.
        vp = open(os.path.join(d, "25-vlan-ports.xml")).read()
        check("te2" not in vp, "backplane ports are not in the VLAN replay")

        # A LAG member that is shut and in LACP mode must still be saved -
        # this is the bug that lost LAG membership in 0.0.1-0.0.3.
        lag = open(os.path.join(d, "15-lag.xml")).read()
        check("<interfaceName>gi1</interfaceName>" in lag,
              "a bound-but-shut LACP member is still saved")

        sw.posts.clear()
        res = t.apply_config(sw, d)
        check(len(res) == len(written), "apply posts every saved file")
        check(all(good for _, good in res), "every replayed file reports OK")
        check([f for f, _ in res] == sorted(f for f, _ in res),
              "files are replayed in filename order")


def test_config_defaults_are_not_saved():
    print("an unconfigured area writes no file")
    sw = FakeSwitch()
    with tempfile.TemporaryDirectory() as d:
        names = [os.path.basename(f) for f in t.save_config(sw, d)]
        # The fixture switch is a plain L2 box: STP off, QoS disabled,
        # 802.1X off, snooping off, no ACL bindings changed. None of those
        # should produce a file, or the config directory becomes noise.
        for unwanted in ("35-stp-global.xml", "36-stp-bridge.xml",
                         "37-stp-ports.xml", "65-qos-global.xml",
                         "71-dot1x-global.xml", "75-multicast.xml",
                         "45-lldp-global.xml", "50-lacp-global.xml",
                         "55-mac-aging.xml", "88-arp-timeout.xml"):
            check(unwanted not in names,
                  f"a default switch does not write {unwanted}")
        # ...but the things the fixture DOES have configured are saved.
        for wanted in ("40-storm.xml", "56-static-mac.xml", "60-acls.xml",
                       "61-aces.xml", "62-acl-bindings.xml", "68-policers.xml",
                       "70-radius.xml", "80-pvlan.xml", "85-gateway.xml",
                       "86-routes.xml", "87-arp.xml"):
            check(wanted in names, f"a configured area writes {wanted}")


def test_config_extended_roundtrip():
    print("extended config replays")
    sw = FakeSwitch()
    with tempfile.TemporaryDirectory() as d:
        written = t.save_config(sw, d)
        for f in written:
            body = open(f).read()
            try:
                root = ET.fromstring(body)
            except ET.ParseError as e:
                check(False, f"{os.path.basename(f)} is malformed: {e}")
                continue
            check(root.tag == "DeviceConfiguration",
                  f"{os.path.basename(f)} is a single DeviceConfiguration")
            # apply_config posts each file as one body, so a file holding
            # two tables would be a request the switch cannot answer.
            tables = [c.tag for c in root if c.tag != "version"]
            check(len(tables) == 1,
                  f"{os.path.basename(f)} holds exactly one table")

        # A secret must not be world-readable.
        radius = os.path.join(d, "70-radius.xml")
        if os.path.exists(radius):
            mode = os.stat(radius).st_mode & 0o777
            check(mode == 0o600, f"70-radius.xml is 0600, not {oct(mode)}")
        check(os.stat(d).st_mode & 0o777 == 0o700,
              "the config directory is 0700")

        # Ordering: an ACE must not replay before its ACL exists.
        names = sorted(os.path.basename(f) for f in written)
        check(names.index("60-acls.xml") < names.index("61-aces.xml")
              < names.index("62-acl-bindings.xml"),
              "ACLs replay before their rules and bindings")
        check(names.index("70-radius.xml") < names.index("80-pvlan.xml"),
              "RADIUS replays before anything that authenticates against it")

        sw.posts.clear()
        res = t.apply_config(sw, d)
        check(all(good for _, good in res),
              "every extended file replays without error")

        # Operational fields must not be echoed back as settings.
        stormfile = os.path.join(d, "40-storm.xml")
        check("portState" not in open(stormfile).read(),
              "read-only operational fields are not replayed")

        # NOTHING replayed may touch a backplane port. te2 carries the
        # management VLAN, and the replay service applies these unattended
        # after every power cycle - a te entry in any of them breaks
        # management on every boot. Observed for real on 2026-08-11 when
        # 11-port-settings.xml included te1-te4.
        for f in written:
            body = open(f).read()
            name = os.path.basename(f)
            for te in ("te1", "te2", "te3", "te4"):
                check(f"<interfaceName>{te}</interfaceName>" not in body,
                      f"{name} does not write to the backplane port {te}")


def test_everything_writable_is_replayed():
    """Anything you can configure must survive a power cycle.

    This is the whole reason the tool exists: the ASIC has no flash, so a
    setting the UI can write but save_config does not capture is silently
    lost on the next AC loss. Rather than spot-check, this asserts that
    every table the UI writes to also appears somewhere in a saved file.
    """
    print("every table the UI can write is also saved")
    sw = FakeSwitch()

    # Drive every write path by exercising the client directly, then see
    # which tables a save produces.
    with tempfile.TemporaryDirectory() as d:
        saved = " ".join(open(f).read() for f in t.save_config(sw, d))

    # Tables the UI's actions post to, gathered from the client methods
    # those actions call. Each must show up in some replay file.
    must_persist = [
        "Standard802_3List",           # port admin, LAG, speed/description
        "VLANList", "VLANName",        # VLANs and their names
        "VLANInterfaceISList",         # port membership
        "PoEPSEInterfaceList",
        "STP", "SpanningTreeGlobalParam",
        "StormControlTable",
        "SpanDestinationTable", "SpanSourceTable",
        "ForwardingStaticTable", "ForwardingGlobalSetting",
        "LLDPGlobalSetting", "LLDPInterfaceList", "CDPInterfaceList",
        "LACPGlobalSetting", "LACPPortList",
        "PrivateVLANTable", "PrivateVLANAssociationTable",
        "ACLList", "ACEList", "ACLBindingList",
        "QoSSettingGlobalParam", "CoSSetting", "CoSToQueueMappingList",
        "AggregatePolicerList",
        "Standard_802_1xGlobalSetting", "Standard_802_1xInterfaceList",
        "RadiusServerList",
        "MulticastGlobalSetting", "IGMPMLDSnoopVLANList",
        "IGMPMLDSnoopRouterPortList",
        "IPv4GlobalSetting", "IPv4GatewayList", "IPv4RouteList", "ARPList",
    ]
    # The fixture switch has these areas switched off, so their files are
    # correctly absent; save them with the area turned on instead.
    on = FakeSwitch()
    restore = dict(FIXTURES)
    for table, frag in {
            "SpanningTreeGlobalParam":
                "<SpanningTreeGlobalParam><enabled>1</enabled>"
                "<STPOperationMode>2</STPOperationMode>"
                "</SpanningTreeGlobalParam>",
            "QoSSettingGlobalParam":
                "<QoSSettingGlobalParam><QoSMode>3</QoSMode>"
                "<basicTrustMode>1</basicTrustMode></QoSSettingGlobalParam>",
            "Standard_802_1xGlobalSetting":
                "<Standard_802_1xGlobalSetting><enabled>1"
                "</enabled></Standard_802_1xGlobalSetting>",
            "MulticastGlobalSetting":
                "<MulticastGlobalSetting><IGMPSnoopEnbl>1"
                "</IGMPSnoopEnbl><multicastFilterEnbl>1"
                "</multicastFilterEnbl></MulticastGlobalSetting>",
            "IPv4GlobalSetting":
                "<IPv4GlobalSetting><unicastRoutingEnable>1"
                "</unicastRoutingEnable></IPv4GlobalSetting>",
            "LLDPGlobalSetting":
                "<LLDPGlobalSetting><LLDPEnabled>2</LLDPEnabled>"
                "<updateInterval>45</updateInterval></LLDPGlobalSetting>",
            "LACPGlobalSetting":
                "<LACPGlobalSetting><LACPSystemPriority>5"
                "</LACPSystemPriority><LAGLoadBalance>2</LAGLoadBalance>"
                "</LACPGlobalSetting>",
            "ForwardingGlobalSetting":
                "<ForwardingGlobalSetting><agingInterval>60</agingInterval>"
                "</ForwardingGlobalSetting>",
            # These four are at their firmware defaults in the base fixture,
            # so they are correctly NOT saved there. Move them off the
            # default so the save path itself is exercised.
            "LLDPInterfaceList":
                "<LLDPInterfaceList><InterfaceEntry>"
                "<interfaceName>gi0</interfaceName><portState>1</portState>"
                "</InterfaceEntry></LLDPInterfaceList>",
            "CDPInterfaceList":
                "<CDPInterfaceList><Entry><interfaceName>gi0</interfaceName>"
                "<enbl>2</enbl></Entry></CDPInterfaceList>",
            "LACPPortList":
                "<LACPPortList><Entry><interfaceName>gi0</interfaceName>"
                "<actorPortPriority>5</actorPortPriority>"
                "<actorPortAdminTimeout>2</actorPortAdminTimeout>"
                "</Entry></LACPPortList>",
            "CoSSetting":
                "<CoSSetting><InterfaceList><Interface>"
                "<interfaceName>gi0</interfaceName><CoS>3</CoS>"
                "</Interface></InterfaceList></CoSSetting>",
    }.items():
        FIXTURES[table] = frag
    try:
        with tempfile.TemporaryDirectory() as d:
            saved += " " + " ".join(
                open(f).read() for f in t.save_config(on, d))
    finally:
        # FIXTURES is module state shared with every other test; leaving it
        # mutated would make later results depend on execution order.
        FIXTURES.clear()
        FIXTURES.update(restore)

    for table in must_persist:
        check(table in saved,
              f"{table} is writable from the UI but never saved - "
              f"it would be lost on a cold power cycle")


def test_menu():
    print("menu navigation")
    sw = FakeSwitch()
    ui = headless_ui(sw)
    rows = ui.menu_rows()
    check(rows[0][0] == "H", "the menu starts with a group heading")
    names = [n for kind, n, _ in rows if kind == "V"]
    check(sorted(names) == sorted(t.MENU_VIEWS),
          "every menu entry is listed once")
    for n in names:
        check(n in t.SPECS, f"menu entry {n} has a spec")
    # Stepping must land on entries, never headings.
    i = ui._menu_step(rows, 1, start=-1)
    check(rows[i][0] == "V", "the first step lands on an entry, not a heading")
    ui.sel = i
    for _ in range(len(rows)):
        ui.sel = ui._menu_step(rows, 1)
        check(rows[ui.sel][0] == "V", "stepping never selects a heading")
    check(ui.sel == max(j for j, r in enumerate(rows) if r[0] == "V"),
          "stepping reaches the last entry in the last group")
    # Whether the last entry is actually VISIBLE needs a real screen -
    # see test_curses_render, which scrolls to it and reads the pixels.


def test_curses_render():
    """Draw every view on a real terminal.

    The checks above never touch curses, so they cannot catch the classic
    TUI bug: an addnstr that runs off the window and raises. This forks a
    child under a pty, draws every view at two window sizes, and reports
    whatever the child throws.
    """
    print("curses rendering")
    import fcntl
    import pty
    import struct
    import termios
    import traceback

    for rows, cols in ((24, 80), (12, 40)):
        pid, fd = pty.fork()
        if pid == 0:                                       # child
            try:
                os.environ["TERM"] = "xterm"
                # UI.__init__ starts a background release check. Tests must
                # not reach the network, and a host running them offline
                # should not wait on a DNS timeout to find that out.
                os.environ["ENCS_NO_UPDATE_CHECK"] = "1"
                import curses as c

                def body(scr):
                    sw = FakeSwitch()
                    ui = t.UI(scr, sw)
                    for view in (["ports", "vlans", "poe", "mac", "stats",
                                  "config", "help", "menu"]
                                 + sorted(t.SPECS)):
                        ui.view = view
                        ui.sel = ui.top = 0
                        ui.msg = "a message long enough to reach the edge " * 3
                        ui.refresh_data()
                        ui.draw()
                        # ...and again with the detail panel off, which is
                        # a different layout, not just a hidden pane.
                        ui.detail = not ui.detail
                        ui.draw()
                        ui.detail = not ui.detail

                    # The menu is taller than a 24-row terminal. Select the
                    # last entry and prove it is on screen - an entry drawn
                    # past the bottom edge is one nobody can find.
                    mrows = ui.menu_rows()
                    last = max(j for j, r in enumerate(mrows) if r[0] == "V")
                    ui.view, ui.sel, ui.top = "menu", last, 0
                    ui.msg = ""
                    ui.draw()
                    height, width = scr.getmaxyx()
                    painted = "\n".join(
                        scr.instr(yy, 0, width).decode("utf8", "replace")
                        for yy in range(height))
                    name = mrows[last][1]
                    assert name in painted, (
                        f"menu entry {name!r} is selected but not drawn "
                        f"at {height}x{width} - the menu is not scrolling")
                c.wrapper(body)
                os._exit(0)
            except BaseException:
                traceback.print_exc()
                sys.stdout.flush()
                os._exit(1)
        # parent: give the child a window with actual dimensions
        fcntl.ioctl(fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))
        out = b""
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                out += chunk
        except OSError:
            pass
        _, status = os.waitpid(pid, 0)
        code = os.WEXITSTATUS(status)
        if code == 0:
            check(True, f"every view draws at {rows}x{cols}")
        else:
            text = out.decode("utf8", "replace")
            # The traceback is the last thing the child printed.
            tail = "\n".join(text.strip().splitlines()[-6:])
            check(False, f"drawing failed at {rows}x{cols}:\n{tail}")


def test_spec_keys():
    print("key bindings")
    seen = {}
    for name, spec in t.SPECS.items():
        for key, action in spec["actions"].items():
            check(hasattr(t.UI, action),
                  f"{name}: {action} exists on UI")
            seen.setdefault(key, []).append(name)
        check("keys" in spec and spec["keys"], f"{name}: has a key bar")
    # A spec key must not shadow a global view hotkey, or that view
    # becomes unreachable from this one.
    globals_ = {k for _, k in t.VIEWS}
    # run() consumes these before a spec ever sees them, so a spec binding
    # one would silently do nothing.
    reserved = {"q", "j", "k", "i", "r", "\t"}
    for name, spec in t.SPECS.items():
        clash = set(spec["actions"]) & globals_
        check(not clash, f"{name}: no key clashes with a view hotkey {clash}")
        eaten = set(spec["actions"]) & reserved
        check(not eaten,
              f"{name}: binds {eaten}, which run() consumes first")


def main():
    for fn in (test_xml_builders, test_span_index, test_fetches_and_render,
               test_missing_tables, test_writes, test_ace_params,
               test_actions, test_vlan_lists, test_vlan_membership_merges,
               test_mirror_guard_without_ports_view, test_no_empty_set_elements,
               test_guards, test_config_roundtrip,
               test_config_defaults_are_not_saved,
               test_config_extended_roundtrip,
               test_everything_writable_is_replayed, test_menu, test_spec_keys,
               test_curses_render):
        fn()
    print(f"\n{CHECKS[0]} checks, {len(FAILURES)} failed")
    for f in FAILURES:
        print(f"  FAILED: {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
