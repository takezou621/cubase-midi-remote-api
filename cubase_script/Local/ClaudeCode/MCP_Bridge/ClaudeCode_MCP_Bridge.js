//-----------------------------------------------------------------------------
// ClaudeCode MCP_Bridge — Cubase / Nuendo MIDI Remote API script
//
// Bridges an MCP server (Python, mido/rtmidi) to Cubase through a virtual
// MIDI port (macOS: IAC Driver Bus 1, Windows: loopMIDI).
//
// Install (macOS):
//   ~/Documents/Steinberg/Cubase/MIDI Remote/Driver Scripts/Local/
//       ClaudeCode/MCP_Bridge/ClaudeCode_MCP_Bridge.js
// Then open the MIDI Remote panel in Cubase and press "Reload Scripts".
//
// Protocol: see PROTOCOL.md in the repository.
// API compatibility: MIDI Remote API v1.0 (Cubase 12+) for everything except
// DirectAccess (v1.2, Cubase 13.0.50+), which is feature-detected.
//-----------------------------------------------------------------------------

//-----------------------------------------------------------------------------
// 0. CONFIG
//-----------------------------------------------------------------------------
var NUM_CHANNELS = 8            // mixer bank slots
var NUM_SENDS = 2               // sends per slot exposed as CC knobs
var NUM_QC = 8                  // Quick Controls (selected track)
var NUM_FX_KNOBS = 8            // focused insert FX parameter knobs
var NUM_KC = 6                  // key command slots
var STR_MAX_UNITS = 32          // max characters per transmitted string
var DA_MAX_DEPTH = 4            // DirectAccess walk depth limit
var DA_MAX_PARAMS_PER_OBJECT = 64
var IDLE_TICKS_PER_SNAPSHOT = 16 // heartbeat: full state resend interval

// Key command slots. Fill with [category, command] pairs copied from the
// Cubase Key Commands dialog. Leave empty (null) to skip a slot.
// Full list for Cubase 13: see "Command Bindings" in the MIDI Remote API docs.
var KC_SLOTS = [
    ['Add Track', 'Instrument'],
    ['Add Track', 'Audio'],
    ['Edit', 'Undo'],
    ['Edit', 'Redo'],
    ['Transport', 'Start'],
    ['Transport', 'Stop']
]

// CC map (channel 0) — must match PROTOCOL.md and mcp_server/protocol.py
var CC = {
    volumeBase: 0,      // 0..7   slot volume
    bankPrev: 8, bankNext: 9, bankShiftLeft: 10, bankShiftRight: 11, bankReset: 12,
    trackPrev: 13, trackNext: 14,
    panBase: 16,        // 16..23 slot pan
    muteBase: 24,       // 24..31 slot mute (toggle press)
    soloBase: 32,       // 32..39 slot solo
    recarmBase: 40,     // 40..47 slot record arm
    monitorBase: 48,    // 48..55 slot monitor enable
    selectBase: 56,     // 56..63 slot select
    sendBase: [64, 72], // send n level: 64..71, 72..79
    fxSlotPrev: 80, fxSlotNext: 81, fxParamBankPrev: 82, fxParamBankNext: 83,
    play: 89, stop: 90, record: 91, cycle: 92, metronome: 93, rewind: 94, forward: 95,
    qcBase: 96,         // 96..103 quick controls (selected track)
    fxBankBase: 104,    // 104..111 focused insert FX parameter knobs
    kcBase: 112         // 112..117 key command slots
}

// SysEx command ids (namespace 0x7D) — must match PROTOCOL.md.
// NOTE: ids are sysex data bytes, so all of them must stay <= 0x7F.
// 0x01-0x1F = MCP -> Cubase commands, 0x20-0x3F = Cubase -> MCP feedback.
var SX = {
    // in
    SELECT_SLOT: 0x01, KC_SLOT: 0x02,
    SET_VOLUME: 0x03, SET_PAN: 0x04, SET_SEND: 0x05,
    SET_MUTE: 0x06, SET_SOLO: 0x07, SET_RECARM: 0x08, SET_MONITOR: 0x09,
    SET_QC: 0x0A, LOCATE: 0x0B, SET_TEMPO: 0x0C,
    DA_WALK: 0x0D, DA_SET: 0x0E, DA_GET: 0x0F,
    BANK: 0x10, PING: 0x11, SNAPSHOT: 0x12, DESELECT_SLOT: 0x13, SELECTED_NAME: 0x14,
    // out
    TRANSPORT: 0x20, SLOT: 0x21, QC: 0x22, PLUGIN: 0x23, DA_PARAM_CHANGE: 0x24,
    DA_OBJ: 0x25, DA_PARAM: 0x26, DA_DONE: 0x27, SELECTED_NAME_OUT: 0x28, IDENT: 0x6E
}

var IDENT_STRING = 'ClaudeCode MCP_Bridge v1'

//-----------------------------------------------------------------------------
// 1. DRIVER SETUP
//-----------------------------------------------------------------------------
var midiremote_api = require('midiremote_api_v1')

var deviceDriver = midiremote_api.makeDeviceDriver('ClaudeCode', 'MCP_Bridge', 'Claude Code MCP bridge')

var midiInput = deviceDriver.mPorts.makeMidiInput('MCP Bridge In')
var midiOutput = deviceDriver.mPorts.makeMidiOutput('MCP Bridge Out')

// Auto-detection: match the virtual MIDI ports (IAC on macOS, loopMIDI on
// Windows, or anything named like this bridge).
deviceDriver.makeDetectionUnit().detectPortPair(midiInput, midiOutput)
    .expectInputNameContains('IAC Driver Bus')
    .expectOutputNameContains('IAC Driver Bus')
deviceDriver.makeDetectionUnit().detectPortPair(midiInput, midiOutput)
    .expectInputNameEquals('Bus 1')
    .expectOutputNameEquals('Bus 1')
deviceDriver.makeDetectionUnit().detectPortPair(midiInput, midiOutput)
    .expectInputNameContains('loopMIDI')
    .expectOutputNameContains('loopMIDI')
deviceDriver.makeDetectionUnit().detectPortPair(midiInput, midiOutput)
    .expectInputNameContains('MCP Bridge')
    .expectOutputNameContains('MCP Bridge')

//-----------------------------------------------------------------------------
// 2. HELPERS — sysex encoding, state cache
//-----------------------------------------------------------------------------
var surface = deviceDriver.mSurface

function outSysex(activeDevice, cmd, payload) {
    var bytes = [0xF0, 0x7D, cmd].concat(payload)
    bytes.push(0xF7)
    midiOutput.sendMidi(activeDevice, bytes)
}

// 14-bit value as two 7-bit bytes, big-endian
function pushU14(arr, value) {
    var v = Math.round(value)
    if (v < 0) v = 0
    if (v > 16383) v = 16383
    arr.push((v >> 7) & 0x7F)
    arr.push(v & 0x7F)
}

// 21-bit value as three 7-bit bytes (parameter tags)
function pushU21(arr, value) {
    var v = value | 0
    if (v < 0) v = 0
    arr.push((v >> 14) & 0x7F)
    arr.push((v >> 7) & 0x7F)
    arr.push(v & 0x7F)
}

// String as [mode][unitLen][data...].
// mode 0: ASCII bytes (1 byte per unit)
// mode 1: UTF-16 code units as 21-bit triples (lossless for Japanese etc.)
function pushString(arr, str, maxUnits) {
    var s = str ? String(str) : ''
    var isAscii = true
    for (var i = 0; i < s.length; ++i) {
        if (s.charCodeAt(i) > 0x7F) { isAscii = false; break }
    }
    var units = Math.min(s.length, maxUnits)
    arr.push(isAscii ? 0 : 1)
    arr.push(units)
    if (isAscii) {
        for (var j = 0; j < units; ++j) arr.push(s.charCodeAt(j) & 0x7F)
    } else {
        for (var k = 0; k < units; ++k) pushU21(arr, s.charCodeAt(k))
    }
}

function pushFlagString(arr, value, str) {
    arr.push(value ? 1 : 0)
    pushString(arr, str, STR_MAX_UNITS)
}

function u14(data, idx) {
    return ((data[idx] & 0x7F) << 7) | (data[idx + 1] & 0x7F)
}

function u21(data, idx) {
    return ((data[idx] & 0x7F) << 14) | ((data[idx + 1] & 0x7F) << 7) | (data[idx + 2] & 0x7F)
}

function decodeString(data, idx) {
    // returns { value: string, next: index }
    var mode = data[idx]
    var units = data[idx + 1]
    var out = ''
    var pos = idx + 2
    if (mode === 0) {
        for (var i = 0; i < units; ++i) out += String.fromCharCode(data[pos + i])
        pos += units
    } else {
        for (var j = 0; j < units; ++j) {
            out += String.fromCharCode(((data[pos] & 0x7F) << 14) | ((data[pos + 1] & 0x7F) << 7) | (data[pos + 2] & 0x7F))
            pos += 3
        }
    }
    return { value: out, next: pos }
}

//-----------------------------------------------------------------------------
// State cache (mirrored on the Python side)
//-----------------------------------------------------------------------------
var gSlots = []
for (var si = 0; si < NUM_CHANNELS; ++si) {
    gSlots.push({
        name: '', vol: -1, pan: -1, volSeen: false,
        selected: 0, mute: 0, solo: 0, recarm: 0, monitor: 0
    })
}
var gSlotDirty = []
for (var di = 0; di < NUM_CHANNELS; ++di) gSlotDirty.push(true)

var gTransport = { play: 0, rec: 0, cycle: 0, metro: 0, bpm: -1, locator: '', locatorFmt: '' }
var gTransportDirty = true

var gQC = []
var gQCDirty = []
for (var qi = 0; qi < NUM_QC; ++qi) { gQC.push(-1); gQCDirty.push(true) }

var gPlugin = { role: 0, name: '', vendor: '', version: '' }
var gPluginDirty = true

var gActiveMapping = null
var gIdleCount = 0
var gSnapshotPending = true

//-----------------------------------------------------------------------------
// 3. SURFACE LAYOUT + 4. HOST MAPPING
//-----------------------------------------------------------------------------
var page = deviceDriver.mMapping.makePage('MCP Bridge')

var hostMixerBankZone = page.mHostAccess.mMixConsole.makeMixerBankZone('MCP Bank')
    .excludeInputChannels()
    .excludeOutputChannels()
hostMixerBankZone.setFollowVisibility(false) // reach even channels hidden in the MixConsole

var selChannel = page.mHostAccess.mTrackSelection.mMixerChannel
var transport = page.mHostAccess.mTransport

// --- one mixer bank strip per slot -------------------------------------------
var strips = []

function makeStrip(channelIndex, x, channel) {
    var strip = { channel: channel }

    strip.label = surface.makeLabelField(x, 0, 2, 1)
    page.setLabelFieldHostObject(strip.label, channel)

    strip.btnSelect = surface.makeButton(x, 1, 2, 1).setTypeToggle()
    strip.btnMute = surface.makeButton(x, 2, 2, 1).setTypeToggle()
    strip.btnSolo = surface.makeButton(x, 3, 2, 1).setTypeToggle()
    strip.btnRecarm = surface.makeButton(x, 4, 2, 1).setTypeToggle()
    strip.btnMonitor = surface.makeButton(x, 5, 2, 1).setTypeToggle()

    strip.fader = surface.makeFader(x, 6, 2, 6).setTypeVertical()
    strip.panKnob = surface.makeKnob(x, 12.5, 2, 2)
    strip.sendKnobs = []
    for (var s = 0; s < NUM_SENDS; ++s) {
        strip.sendKnobs.push(surface.makeKnob(x, 14.5 + s * 2, 2, 2))
    }

    // --- MIDI bindings ---
    strip.btnSelect.mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
        .bindToControlChange(0, CC.selectBase + channelIndex)
    strip.btnMute.mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
        .bindToControlChange(0, CC.muteBase + channelIndex)
    strip.btnSolo.mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
        .bindToControlChange(0, CC.soloBase + channelIndex)
    strip.btnRecarm.mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
        .bindToControlChange(0, CC.recarmBase + channelIndex)
    strip.btnMonitor.mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
        .bindToControlChange(0, CC.monitorBase + channelIndex)
    strip.fader.mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
        .bindToControlChange(0, CC.volumeBase + channelIndex)
    strip.panKnob.mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
        .bindToControlChange(0, CC.panBase + channelIndex)
    for (var s2 = 0; s2 < NUM_SENDS; ++s2) {
        strip.sendKnobs[s2].mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
            .bindToControlChange(0, CC.sendBase[s2] + channelIndex)
    }

    // --- host bindings ---
    page.makeValueBinding(strip.btnSelect.mSurfaceValue, strip.channel.mValue.mSelected).setTypeToggle()
    page.makeValueBinding(strip.btnMute.mSurfaceValue, strip.channel.mValue.mMute).setTypeToggle()
    page.makeValueBinding(strip.btnSolo.mSurfaceValue, strip.channel.mValue.mSolo).setTypeToggle()
    page.makeValueBinding(strip.btnRecarm.mSurfaceValue, strip.channel.mValue.mRecordEnable).setTypeToggle()
    page.makeValueBinding(strip.btnMonitor.mSurfaceValue, strip.channel.mValue.mMonitorEnable).setTypeToggle()
    page.makeValueBinding(strip.fader.mSurfaceValue, strip.channel.mValue.mVolume).setValueTakeOverModeJump()
    page.makeValueBinding(strip.panKnob.mSurfaceValue, strip.channel.mValue.mPan).setValueTakeOverModeJump()
    for (var s3 = 0; s3 < NUM_SENDS; ++s3) {
        page.makeValueBinding(strip.sendKnobs[s3].mSurfaceValue, strip.channel.mSends.getByIndex(s3).mLevel)
    }

    // --- track name -> cache + GUI label ---
    strip.channel.mOnTitleChange = function (activeDevice, activeMapping, title) {
        var s = gSlots[channelIndex]
        if (s.name !== title) {
            s.name = title
            gSlotDirty[channelIndex] = true
        }
    }
    // some host versions only push the initial title through the surface
    // value's own title change (objectTitle = channel name)
    strip.fader.mSurfaceValue.mOnTitleChange = function (activeDevice, objectTitle, valueTitle) {
        var s = gSlots[channelIndex]
        if (objectTitle && objectTitle !== s.name) {
            s.name = objectTitle
            gSlotDirty[channelIndex] = true
        }
    }

    // --- value feedback -> cache ---
    strip.fader.mSurfaceValue.mOnProcessValueChange = function (activeDevice, value) {
        gSlots[channelIndex].vol = Math.round(value * 16383)
        gSlots[channelIndex].volSeen = true
        gSlotDirty[channelIndex] = true
    }
    strip.panKnob.mSurfaceValue.mOnProcessValueChange = function (activeDevice, value) {
        gSlots[channelIndex].pan = Math.round(value * 16383)
        gSlotDirty[channelIndex] = true
    }
    strip.btnSelect.mSurfaceValue.mOnProcessValueChange = function (activeDevice, value) {
        gSlots[channelIndex].selected = value > 0.5 ? 1 : 0
        gSlotDirty[channelIndex] = true
    }
    strip.btnMute.mSurfaceValue.mOnProcessValueChange = function (activeDevice, value) {
        gSlots[channelIndex].mute = value > 0.5 ? 1 : 0
        gSlotDirty[channelIndex] = true
    }
    strip.btnSolo.mSurfaceValue.mOnProcessValueChange = function (activeDevice, value) {
        gSlots[channelIndex].solo = value > 0.5 ? 1 : 0
        gSlotDirty[channelIndex] = true
    }
    strip.btnRecarm.mSurfaceValue.mOnProcessValueChange = function (activeDevice, value) {
        gSlots[channelIndex].recarm = value > 0.5 ? 1 : 0
        gSlotDirty[channelIndex] = true
    }
    strip.btnMonitor.mSurfaceValue.mOnProcessValueChange = function (activeDevice, value) {
        gSlots[channelIndex].monitor = value > 0.5 ? 1 : 0
        gSlotDirty[channelIndex] = true
    }

    return strip
}

for (var ci = 0; ci < NUM_CHANNELS; ++ci) {
    var bankChannel = hostMixerBankZone.makeMixerBankChannel()
    strips.push(makeStrip(ci, ci * 2, bankChannel))
}

// --- transport ----------------------------------------------------------------
function makeTransportButton(x, label) {
    var btn = surface.makeButton(x, 19, 2, 1)
    var lbl = surface.makeLabelField(x, 20, 2, 1)
    page.setLabelFieldText(lbl, label)
    btn.mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
    return { btn: btn }
}

var tPlay = makeTransportButton(0, 'Play')
var tStop = makeTransportButton(2, 'Stop')
var tRecord = makeTransportButton(4, 'Rec')
var tCycle = makeTransportButton(6, 'Cycle')
var tMetro = makeTransportButton(8, 'Click')
var tRewind = makeTransportButton(10, 'Rew')
var tForward = makeTransportButton(12, 'Fwd')

page.makeValueBinding(tPlay.btn.mSurfaceValue, transport.mValue.mStart).setTypeToggle()
page.makeValueBinding(tStop.btn.mSurfaceValue, transport.mValue.mStop).setTypeToggle()
page.makeValueBinding(tRecord.btn.mSurfaceValue, transport.mValue.mRecord).setTypeToggle()
page.makeValueBinding(tCycle.btn.mSurfaceValue, transport.mValue.mCycleActive).setTypeToggle()
page.makeValueBinding(tMetro.btn.mSurfaceValue, transport.mValue.mMetronomeActive).setTypeToggle()
page.makeValueBinding(tRewind.btn.mSurfaceValue, transport.mValue.mRewind)
page.makeValueBinding(tForward.btn.mSurfaceValue, transport.mValue.mForward)

// transport buttons are driven from the MCP server via CCs (89..95, press = 127)
bindCC(tPlay.btn.mSurfaceValue, CC.play)
bindCC(tStop.btn.mSurfaceValue, CC.stop)
bindCC(tRecord.btn.mSurfaceValue, CC.record)
bindCC(tCycle.btn.mSurfaceValue, CC.cycle)
bindCC(tMetro.btn.mSurfaceValue, CC.metronome)
bindCC(tRewind.btn.mSurfaceValue, CC.rewind)
bindCC(tForward.btn.mSurfaceValue, CC.forward)

function watchTransport(btnObj, key) {
    btnObj.btn.mSurfaceValue.mOnProcessValueChange = function (activeDevice, value) {
        var v = value > 0.5 ? 1 : 0
        if (gTransport[key] !== v) {
            gTransport[key] = v
            gTransportDirty = true
        }
    }
}
watchTransport(tPlay, 'play')
watchTransport(tRecord, 'rec')
watchTransport(tCycle, 'cycle')
watchTransport(tMetro, 'metro')

// --- bank / track navigation ---------------------------------------------------
function makeNavButton(x, y, label) {
    var btn = surface.makeButton(x, y, 2, 1)
    page.setLabelFieldText(surface.makeLabelField(x, y + 1, 2, 1), label)
    btn.mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
    return btn
}

var navBankPrev = makeNavButton(0, 22, 'Bank-')
var navBankNext = makeNavButton(2, 22, 'Bank+')
var navShiftLeft = makeNavButton(4, 22, 'ShL')
var navShiftRight = makeNavButton(6, 22, 'ShR')
var navBankReset = makeNavButton(8, 22, 'Rst')
var navTrackPrev = makeNavButton(10, 22, 'Tr-')
var navTrackNext = makeNavButton(12, 22, 'Tr+')

page.makeActionBinding(navBankPrev.mSurfaceValue, hostMixerBankZone.mAction.mPrevBank)
page.makeActionBinding(navBankNext.mSurfaceValue, hostMixerBankZone.mAction.mNextBank)
page.makeActionBinding(navShiftLeft.mSurfaceValue, hostMixerBankZone.mAction.mShiftLeft)
page.makeActionBinding(navShiftRight.mSurfaceValue, hostMixerBankZone.mAction.mShiftRight)
page.makeActionBinding(navBankReset.mSurfaceValue, hostMixerBankZone.mAction.mResetBank)
page.makeActionBinding(navTrackPrev.mSurfaceValue, page.mHostAccess.mTrackSelection.mAction.mPrevTrack)
page.makeActionBinding(navTrackNext.mSurfaceValue, page.mHostAccess.mTrackSelection.mAction.mNextTrack)

// bind nav buttons to CCs so the MCP server can trigger them
function bindCC(elementValue, cc) {
    elementValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput).bindToControlChange(0, cc)
}
bindCC(navBankPrev.mSurfaceValue, CC.bankPrev)
bindCC(navBankNext.mSurfaceValue, CC.bankNext)
bindCC(navShiftLeft.mSurfaceValue, CC.bankShiftLeft)
bindCC(navShiftRight.mSurfaceValue, CC.bankShiftRight)
bindCC(navBankReset.mSurfaceValue, CC.bankReset)
bindCC(navTrackPrev.mSurfaceValue, CC.trackPrev)
bindCC(navTrackNext.mSurfaceValue, CC.trackNext)

// --- Quick Controls (selected track) -------------------------------------------
var qcKnobs = []
for (var qk = 0; qk < NUM_QC; ++qk) {
    var qcKnob = surface.makeKnob(qk * 2, 24.5, 2, 2)
    qcKnob.mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
        .bindToControlChange(0, CC.qcBase + qk)
    page.makeValueBinding(qcKnob.mSurfaceValue, selChannel.mQuickControls.getByIndex(qk))
    qcKnob.mSurfaceValue.mOnProcessValueChange = (function (idx) {
        return function (activeDevice, value) {
            gQC[idx] = Math.round(value * 16383)
            gQCDirty[idx] = true
        }
    })(qk)
    qcKnobs.push(qcKnob)
}
page.setLabelFieldText(surface.makeLabelField(16, 24.5, 4, 1), 'QC (sel.)')

// --- focused insert FX parameters (Direct Access) --------------------------------
var fxViewer = selChannel.mInsertAndStripEffects.makeInsertEffectViewer('Focus FX')
    .followPluginWindowInFocus()

var fxKnobs = []
for (var fk = 0; fk < NUM_FX_KNOBS; ++fk) {
    var fxKnob = surface.makeKnob(fk * 2, 27, 2, 2)
    fxKnob.mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
        .bindToControlChange(0, CC.fxBankBase + fk)
    page.makeValueBinding(fxKnob.mSurfaceValue, fxViewer.mParameterBankZone.makeParameterValue())
    fxKnobs.push(fxKnob)
}

var fxSlotPrev = makeNavButton(16, 27, 'FX-')
var fxSlotNext = makeNavButton(18, 27, 'FX+')
var fxParamBankPrev = makeNavButton(20, 27, 'PB-')
var fxParamBankNext = makeNavButton(22, 27, 'PB+')
page.makeActionBinding(fxSlotPrev.mSurfaceValue, fxViewer.mAction.mPrev)
page.makeActionBinding(fxSlotNext.mSurfaceValue, fxViewer.mAction.mNext)
page.makeActionBinding(fxParamBankPrev.mSurfaceValue, fxViewer.mParameterBankZone.mAction.mPrevBank)
page.makeActionBinding(fxParamBankNext.mSurfaceValue, fxViewer.mParameterBankZone.mAction.mNextBank)
bindCC(fxSlotPrev.mSurfaceValue, CC.fxSlotPrev)
bindCC(fxSlotNext.mSurfaceValue, CC.fxSlotNext)
bindCC(fxParamBankPrev.mSurfaceValue, CC.fxParamBankPrev)
bindCC(fxParamBankNext.mSurfaceValue, CC.fxParamBankNext)

fxViewer.mOnChangePluginIdentity = function (activeDevice, activeMapping, pluginName, pluginVendor, pluginVersion, formatVersion) {
    gPlugin.role = 2
    gPlugin.name = pluginName
    gPlugin.vendor = pluginVendor
    gPlugin.version = pluginVersion
    gPluginDirty = true
}
selChannel.mInstrumentPluginSlot.mOnChangePluginIdentity = function (activeDevice, activeMapping, pluginName, pluginVendor, pluginVersion, formatVersion) {
    gPlugin.role = 1
    gPlugin.name = pluginName
    gPlugin.vendor = pluginVendor
    gPlugin.version = pluginVersion
    gPluginDirty = true
}

// --- key command slots ------------------------------------------------------------
var kcButtons = []
for (var kk = 0; kk < NUM_KC; ++kk) {
    var kcBtn = surface.makeButton(kk * 2, 30, 2, 1)
    page.setLabelFieldText(surface.makeLabelField(kk * 2, 31, 2, 1), 'KC ' + kk)
    kcBtn.mSurfaceValue.mMidiBinding.setInputPort(midiInput).setOutputPort(midiOutput)
        .bindToControlChange(0, CC.kcBase + kk)
    if (KC_SLOTS[kk] && KC_SLOTS[kk].length >= 2) {
        page.makeCommandBinding(kcBtn.mSurfaceValue, KC_SLOTS[kk][0], KC_SLOTS[kk][1])
    }
    kcButtons.push(kcBtn)
}

// --- transport time display -> cache ------------------------------------------------
transport.mTimeDisplay.mPrimary.mTransportLocator.mOnChange = function (activeDevice, activeMapping, time, format) {
    if (gTransport.locator !== time || gTransport.locatorFmt !== format) {
        gTransport.locator = time
        gTransport.locatorFmt = format
        gTransportDirty = true
    }
}
transport.mTimeDisplay.mOnChangeTempoBPM = function (activeDevice, activeMapping, tempoBPM) {
    var b = Math.round(tempoBPM * 10)
    if (gTransport.bpm !== b) {
        gTransport.bpm = b
        gTransportDirty = true
    }
}

//-----------------------------------------------------------------------------
// 5. FEEDBACK — send cached state
//-----------------------------------------------------------------------------
function sendSlotInfo(activeDevice, slotIndex) {
    var s = gSlots[slotIndex]
    var flags = 0
    if (s.selected) flags |= 1
    if (s.mute) flags |= 2
    if (s.solo) flags |= 4
    if (s.recarm) flags |= 8
    if (s.monitor) flags |= 16
    if (s.name.length > 0 || s.volSeen) flags |= 32
    // NOTE: outSysex() already prepends the command byte — payload starts after it
    var payload = [slotIndex, flags]
    pushU14(payload, s.vol < 0 ? 0 : s.vol)
    pushU14(payload, s.pan < 0 ? 8192 : s.pan)
    pushFlagString(payload, true, s.name)
    outSysex(activeDevice, SX.SLOT, payload)
}

function sendTransportInfo(activeDevice) {
    var flags = (gTransport.play ? 1 : 0) | (gTransport.rec ? 2 : 0) |
        (gTransport.cycle ? 4 : 0) | (gTransport.metro ? 8 : 0)
    var payload = [flags]
    pushU14(payload, gTransport.bpm < 0 ? 0 : gTransport.bpm)
    pushFlagString(payload, true, gTransport.locator)
    pushFlagString(payload, true, gTransport.locatorFmt)
    outSysex(activeDevice, SX.TRANSPORT, payload)
}

function sendQCInfo(activeDevice, idx) {
    var payload = [idx]
    pushU14(payload, gQC[idx] < 0 ? 0 : gQC[idx])
    outSysex(activeDevice, SX.QC, payload)
}

function sendPluginInfo(activeDevice) {
    var payload = [gPlugin.role]
    pushString(payload, gPlugin.name, STR_MAX_UNITS)
    pushString(payload, gPlugin.vendor, STR_MAX_UNITS)
    pushString(payload, gPlugin.version, STR_MAX_UNITS)
    outSysex(activeDevice, SX.PLUGIN, payload)
}

function sendIdent(activeDevice) {
    var payload = []
    pushString(payload, IDENT_STRING, STR_MAX_UNITS)
    outSysex(activeDevice, SX.IDENT, payload)
}

function sendSnapshot(activeDevice) {
    sendIdent(activeDevice)
    sendTransportInfo(activeDevice)
    for (var i = 0; i < NUM_CHANNELS; ++i) sendSlotInfo(activeDevice, i)
    for (var q = 0; q < NUM_QC; ++q) sendQCInfo(activeDevice, q)
    sendPluginInfo(activeDevice)
}

//-----------------------------------------------------------------------------
// 6. DIRECTACCESS (API v1.2 / Cubase 13.0.50+, feature-detected)
//-----------------------------------------------------------------------------
var da = null
if (page.mHostAccess.makeDirectAccess) {
    da = page.mHostAccess.makeDirectAccess(selChannel)
}

var gDaWalkId = 0
var gDaSeqToObjectID = []   // seq -> objectID (last walk)
var gDaObjectIDToSeq = {}   // objectID -> seq (last walk)

function daWalk(activeDevice, limit) {
    if (!da || !gActiveMapping) return
    var am = gActiveMapping
    da.update(am)
    gDaSeqToObjectID = []
    gDaObjectIDToSeq = {}
    var walkId = ++gDaWalkId % 128
    var count = 0

    var queue = []
    var root = da.getBaseObjectID(am)
    if (root < 0) {
        outSysex(activeDevice, SX.DA_DONE, [walkId, 0, 0])
        return
    }
    queue.push({ id: root, parent: 16383, depth: 0 })

    while (queue.length > 0 && count < limit) {
        var node = queue.shift()
        var seq = count
        var title = ''
        var typeName = ''
        try { title = da.getObjectTitle(am, node.id) } catch (e) { }
        try { typeName = da.getObjectTypeName(am, node.id) } catch (e) { }

        var payload = [walkId]
        pushU14(payload, seq)
        pushU14(payload, node.parent)
        pushFlagString(payload, true, typeName)
        pushFlagString(payload, true, title)
        outSysex(activeDevice, SX.DA_OBJ, payload)

        gDaSeqToObjectID[seq] = node.id
        gDaObjectIDToSeq[node.id] = seq
        count++

        if (node.depth < DA_MAX_DEPTH) {
            var childCount = 0
            try { childCount = da.getNumberOfChildObjects(am, node.id) } catch (e) { }
            for (var c = 0; c < childCount && count < limit; ++c) {
                var childID = da.getChildObjectID(am, node.id, c)
                queue.push({ id: childID, parent: seq, depth: node.depth + 1 })
            }
        }

        var paramCount = 0
        try { paramCount = da.getNumberOfParameters(am, node.id) } catch (e) { }
        if (paramCount > DA_MAX_PARAMS_PER_OBJECT) paramCount = DA_MAX_PARAMS_PER_OBJECT
        for (var p = 0; p < paramCount && count < limit; ++p) {
            var tag = da.getParameterTagByIndex(am, node.id, p)
            var pTitle = ''
            var pDisplay = ''
            var pValue = 0
            try { pTitle = da.getParameterTitle(am, node.id, tag, 48) } catch (e) { }
            try { pDisplay = da.getParameterDisplayValue(am, node.id, tag) } catch (e) { }
            try { pValue = da.getParameterProcessValue(am, node.id, tag) } catch (e) { }

            var pPayload = [walkId]
            pushU14(pPayload, seq)
            pushU21(pPayload, tag)
            pushU14(pPayload, Math.round(pValue * 16383))
            pushFlagString(pPayload, true, pTitle)
            pushFlagString(pPayload, true, pDisplay)
            outSysex(activeDevice, SX.DA_PARAM, pPayload)
            count++
        }
    }
    outSysex(activeDevice, SX.DA_DONE, [walkId, (count >> 7) & 0x7F, count & 0x7F])
}

if (da) {
    da.mOnParameterChange = function (activeDevice, activeMapping, objectID, parameterTag) {
        var seq = gDaObjectIDToSeq[objectID]
        if (seq === undefined) return
        var pv = null
        try { pv = da.getParameterProcessValue(activeMapping, objectID, parameterTag) } catch (e) { }
        if (pv === null) return // read failed: stay silent instead of sending a bogus 0
        var payload = [SX.DA_PARAM_CHANGE]
        pushU14(payload, seq)
        pushU21(payload, parameterTag)
        pushU14(payload, Math.round(pv * 16383))
        outSysex(activeDevice, SX.DA_PARAM_CHANGE, payload)
    }
}

//-----------------------------------------------------------------------------
// 7. SYSEX COMMAND HANDLER (MCP -> script)
//-----------------------------------------------------------------------------
function pulseButton(activeDevice, btn) {
    btn.mSurfaceValue.setProcessValue(activeDevice, 1)
    btn.mSurfaceValue.setProcessValue(activeDevice, 0)
}

// set a toggle-typed button to a desired state.
// feedback keeps the surface value latched at the host state, so a plain
// set(1) would not create a 0->1 edge — simulate a full click instead.
function toggleTo(activeDevice, btn, desiredOn) {
    var cur = btn.mSurfaceValue.getProcessValue(activeDevice) > 0.5
    if (cur !== (desiredOn > 0.5)) {
        btn.mSurfaceValue.setProcessValue(activeDevice, 0)
        btn.mSurfaceValue.setProcessValue(activeDevice, 1)
        btn.mSurfaceValue.setProcessValue(activeDevice, 0)
    }
}

midiInput.mOnSysex = function (activeDevice, message) {
    // message = [0xF0, 0x7D, cmd, payload..., 0xF7]; tolerate stripped F0/F7
    if (message.length < 3) return
    var offset = message[0] === 0xF0 ? 1 : 0
    var end = message.length
    if (message[end - 1] === 0xF7) end--
    if (message[offset] !== 0x7D) return
    var cmd = message[offset + 1]
    var data = []
    for (var i = offset + 2; i < end; ++i) data.push(message[i])

    switch (cmd) {
        case SX.PING: {
            // answer with a full snapshot (starts with IDENT) so a freshly
            // connected MCP server gets complete state immediately
            sendSnapshot(activeDevice)
            break
        }
        case SX.SNAPSHOT: {
            gSnapshotPending = true
            break
        }
        case SX.SELECT_SLOT: {
            var slot = data[0]
            if (slot >= NUM_CHANNELS) return
            for (var s = 0; s < NUM_CHANNELS; ++s) {
                if (s !== slot && gSlots[s].selected) toggleTo(activeDevice, strips[s].btnSelect, false)
            }
            toggleTo(activeDevice, strips[slot].btnSelect, true)
            break
        }
        case SX.DESELECT_SLOT: {
            var dslot = data[0]
            if (dslot < NUM_CHANNELS) toggleTo(activeDevice, strips[dslot].btnSelect, false)
            break
        }
        case SX.KC_SLOT: {
            var k = data[0]
            if (k < NUM_KC) pulseButton(activeDevice, kcButtons[k])
            break
        }
        case SX.SET_VOLUME: {
            var slot2 = data[0]
            if (slot2 >= NUM_CHANNELS) return
            strips[slot2].fader.mSurfaceValue.setProcessValue(activeDevice, u14(data, 1) / 16383)
            break
        }
        case SX.SET_PAN: {
            var slot3 = data[0]
            if (slot3 >= NUM_CHANNELS) return
            strips[slot3].panKnob.mSurfaceValue.setProcessValue(activeDevice, u14(data, 1) / 16383)
            break
        }
        case SX.SET_SEND: {
            var slot4 = data[0]
            var sendIdx = data[1]
            if (slot4 >= NUM_CHANNELS || sendIdx >= NUM_SENDS) return
            strips[slot4].sendKnobs[sendIdx].mSurfaceValue.setProcessValue(activeDevice, u14(data, 2) / 16383)
            break
        }
        case SX.SET_MUTE: {
            if (data[0] < NUM_CHANNELS) toggleTo(activeDevice, strips[data[0]].btnMute, data[1])
            break
        }
        case SX.SET_SOLO: {
            if (data[0] < NUM_CHANNELS) toggleTo(activeDevice, strips[data[0]].btnSolo, data[1])
            break
        }
        case SX.SET_RECARM: {
            if (data[0] < NUM_CHANNELS) toggleTo(activeDevice, strips[data[0]].btnRecarm, data[1])
            break
        }
        case SX.SET_MONITOR: {
            if (data[0] < NUM_CHANNELS) toggleTo(activeDevice, strips[data[0]].btnMonitor, data[1])
            break
        }
        case SX.SET_QC: {
            var qi = data[0]
            if (qi >= NUM_QC) return
            qcKnobs[qi].mSurfaceValue.setProcessValue(activeDevice, u14(data, 1) / 16383)
            break
        }
        case SX.LOCATE: {
            var t = decodeString(data, 0)
            if (gActiveMapping && transport.mTimeDisplay.mPrimary.mTransportLocator.setTime) {
                transport.mTimeDisplay.mPrimary.mTransportLocator.setTime(gActiveMapping, t.value)
            }
            break
        }
        case SX.SET_TEMPO: {
            if (gActiveMapping && transport.mTimeDisplay.setTempoBPM) {
                transport.mTimeDisplay.setTempoBPM(gActiveMapping, u14(data, 0) / 10)
            }
            break
        }
        case SX.DA_WALK: {
            var limit = u14(data, 0)
            if (limit <= 0) limit = 80
            daWalk(activeDevice, limit)
            break
        }
        case SX.DA_SET: {
            if (!da || !gActiveMapping) return
            var seq1 = u14(data, 0)
            var tag1 = u21(data, 2)
            var obj1 = gDaSeqToObjectID[seq1]
            if (obj1 !== undefined) {
                try { da.setParameterProcessValue(gActiveMapping, obj1, tag1, u14(data, 5) / 16383) } catch (e) { }
            }
            break
        }
        case SX.DA_GET: {
            if (!da || !gActiveMapping) return
            var seq2 = u14(data, 0)
            var tag2 = u21(data, 2)
            var obj2 = gDaSeqToObjectID[seq2]
            if (obj2 !== undefined) {
                var pv2 = null
                try { pv2 = da.getParameterProcessValue(gActiveMapping, obj2, tag2) } catch (e) { }
                if (pv2 === null) break // read failed: stay silent instead of sending a bogus 0
                var payload2 = [SX.DA_PARAM_CHANGE]
                pushU14(payload2, seq2)
                pushU21(payload2, tag2)
                pushU14(payload2, Math.round(pv2 * 16383))
                outSysex(activeDevice, SX.DA_PARAM_CHANGE, payload2)
            }
            break
        }
        case SX.SELECTED_NAME: {
            // DirectAccess title of the selected track (needs API v1.2+)
            if (!da || !gActiveMapping) return
            da.update(gActiveMapping) // refresh runtime IDs before reading
            var sbase = da.getBaseObjectID(gActiveMapping)
            if (sbase < 0) return
            var stitle = ''
            try { stitle = da.getObjectTitle(gActiveMapping, sbase) } catch (e) { }
            var spl = []
            pushString(spl, stitle, STR_MAX_UNITS)
            outSysex(activeDevice, SX.SELECTED_NAME_OUT, spl)
            break
        }
        case SX.BANK: {
            var step = data[0]
            if (step === 0) pulseButton(activeDevice, navBankNext)
            else if (step === 1) pulseButton(activeDevice, navBankPrev)
            else if (step === 2) pulseButton(activeDevice, navShiftLeft)
            else if (step === 3) pulseButton(activeDevice, navShiftRight)
            else if (step === 4) pulseButton(activeDevice, navBankReset)
            break
        }
    }
}

//-----------------------------------------------------------------------------
// 8. PAGE LIFECYCLE + HEARTBEAT
//-----------------------------------------------------------------------------
page.mOnActivate = function (activeDevice, activeMapping) {
    gActiveMapping = activeMapping
    if (da) da.activate(activeMapping)
    gSnapshotPending = true
}

page.mOnDeactivate = function (activeDevice, activeMapping) {
    if (da) da.deactivate(activeMapping)
    gActiveMapping = null
}

page.mOnIdle = function (activeDevice, activeMapping) {
    gIdleCount++

    // flush dirty state as soon as possible
    if (gTransportDirty) {
        sendTransportInfo(activeDevice)
        gTransportDirty = false
    }
    for (var i = 0; i < NUM_CHANNELS; ++i) {
        if (gSlotDirty[i]) {
            sendSlotInfo(activeDevice, i)
            gSlotDirty[i] = false
        }
    }
    for (var q = 0; q < NUM_QC; ++q) {
        if (gQCDirty[q]) {
            sendQCInfo(activeDevice, q)
            gQCDirty[q] = false
        }
    }
    if (gPluginDirty) {
        sendPluginInfo(activeDevice)
        gPluginDirty = false
    }

    // periodic full snapshot (also covers MCP (re)connecting later)
    if (gSnapshotPending || (gIdleCount % IDLE_TICKS_PER_SNAPSHOT) === 0) {
        sendSnapshot(activeDevice)
        gSnapshotPending = false
        for (var i2 = 0; i2 < NUM_CHANNELS; ++i2) gSlotDirty[i2] = false
        for (var q2 = 0; q2 < NUM_QC; ++q2) gQCDirty[q2] = false
        gTransportDirty = false
        gPluginDirty = false
    }
}
