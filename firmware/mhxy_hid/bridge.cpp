#include <Arduino.h>
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include <Preferences.h>
#include <atomic>
#include <esp_random.h>

namespace {
constexpr char name[] = "MHXY iPhone HID";
constexpr uint32_t pairTime = 180000;
constexpr uint16_t noConnection = BLE_HS_CONN_HANDLE_NONE;
constexpr uint8_t keyboardBit = 1, mouseBit = 2, consumerBit = 4;
constexpr int reportDelay = 8;

// HOGP report IDs: keyboard (8 bytes), relative mouse (5), consumer key (2).
uint8_t reportMap[] = {
    0x05,0x01, 0x09,0x06, 0xa1,0x01, 0x85,0x01,
    0x05,0x07, 0x19,0xe0, 0x29,0xe7, 0x15,0x00, 0x25,0x01, 0x75,0x01, 0x95,0x08, 0x81,0x02,
    0x75,0x08, 0x95,0x01, 0x81,0x01,
    0x05,0x08, 0x19,0x01, 0x29,0x05, 0x75,0x01, 0x95,0x05, 0x91,0x02,
    0x75,0x03, 0x95,0x01, 0x91,0x01,
    0x05,0x07, 0x19,0x00, 0x29,0x65, 0x15,0x00, 0x25,0x65, 0x75,0x08, 0x95,0x06, 0x81,0x00, 0xc0,
    0x05,0x01, 0x09,0x02, 0xa1,0x01, 0x85,0x02, 0x09,0x01, 0xa1,0x00,
    0x05,0x09, 0x19,0x01, 0x29,0x03, 0x15,0x00, 0x25,0x01, 0x75,0x01, 0x95,0x03, 0x81,0x02,
    0x75,0x05, 0x95,0x01, 0x81,0x01,
    0x05,0x01, 0x09,0x30, 0x09,0x31, 0x09,0x38, 0x15,0x81, 0x25,0x7f, 0x75,0x08, 0x95,0x03, 0x81,0x06,
    0x05,0x0c, 0x0a,0x38,0x02, 0x95,0x01, 0x81,0x06, 0xc0, 0xc0,
    0x05,0x0c, 0x09,0x01, 0xa1,0x01, 0x85,0x03,
    0x19,0x00, 0x2a,0xff,0x03, 0x15,0x00, 0x26,0xff,0x03, 0x75,0x10, 0x95,0x01, 0x81,0x00, 0xc0,
};

Preferences prefs;
NimBLECharacteristic *keyboard, *mouse, *consumer;
NimBLEAdvertising *advertising;
std::atomic<uint16_t> connection{noConnection};
std::atomic<uint8_t> subscribed{0};
std::atomic<bool> authorized{false}, bonded{false};
std::atomic<uint32_t> pairAt{0}, linkGeneration{0}, connectAt{0};
NimBLEAddress trusted;
uint32_t heldAt = 0, stateGeneration = 0;
uint8_t buttons = 0;

bool pairing() {
    uint32_t at = pairAt.load();
    return at && millis() - at < pairTime;
}

class PeerEvents : public NimBLEServerCallbacks {
    void onConnect(NimBLEServer* server, NimBLEConnInfo& info) override {
        if (connection != noConnection || (!bonded && !pairing())) {
            server->disconnect(info);
            return;
        }
        connection = info.getConnHandle();
        connectAt = millis();
        server->updateConnParams(info.getConnHandle(), 12, 18, 0, 400);
    }

    void onAuthenticationComplete(NimBLEConnInfo& info) override {
        if (info.getConnHandle() != connection) return;
        const auto id = info.getIdAddress();
        if (info.isEncrypted() && info.isBonded()) {
            if (bonded && id == trusted) {
                authorized = true;
                return;
            }
            if (!bonded && pairing() && prefs.putString("peer", id.toString().c_str()) &&
                    prefs.putUChar("peerType", id.getType())) {
                trusted = id;
                bonded = true;
                authorized = true;
                pairAt = 0;
                return;
            }
        }
        NimBLEDevice::getServer()->disconnect(info);
    }

    void onDisconnect(NimBLEServer*, NimBLEConnInfo& info, int) override {
        if (info.getConnHandle() != connection) return;
        authorized = false;
        subscribed = 0;
        connection = noConnection;
        ++linkGeneration;
    }
};

class ReportEvents : public NimBLECharacteristicCallbacks {
    void onSubscribe(NimBLECharacteristic* chr, NimBLEConnInfo& info, uint16_t value) override {
        if (info.getConnHandle() != connection) return;
        uint8_t flag = chr == keyboard ? keyboardBit : chr == mouse ? mouseBit : consumerBit;
        if (value & 1) subscribed.fetch_or(flag);
        else subscribed.fetch_and(uint8_t(~flag));
    }
};

PeerEvents peerEvents;
ReportEvents reportEvents;

// Distribute whole mouse counts over a fixed number of Bluetooth reports.
class AxisSteps {
    int direction, whole, remainder, carry, span;

public:
    AxisSteps(int distance, int reports)
        : direction(distance < 0 ? -1 : 1), whole(abs(distance) / reports),
          remainder(abs(distance) % reports), carry(reports / 2), span(reports) {}

    int next() {
        int counts = whole;
        carry += remainder;
        if (carry >= span) {
            carry -= span;
            ++counts;
        }
        return direction * counts;
    }
};

bool ready(uint8_t bit) {
    return connection != noConnection && authorized && (subscribed.load() & bit) == bit;
}

bool notify(NimBLECharacteristic* chr, uint8_t bit, const uint8_t* data, size_t len) {
    const uint32_t generation = linkGeneration;
    const uint16_t handle = connection;
    for (int tries = 0; tries < 4; ++tries) {
        if (!ready(bit) || generation != linkGeneration || handle != connection) return false;
        chr->setValue(data, len);
        if (chr->notify(data, len, handle)) return true;
        delay(reportDelay);
    }
    return false;
}

bool sendMouse(int x = 0, int y = 0, int wheel = 0, int pan = 0) {
    uint8_t data[] = {buttons, uint8_t(x), uint8_t(y), uint8_t(wheel), uint8_t(pan)};
    return notify(mouse, mouseBit, data, sizeof(data));
}

bool stroke(const uint8_t (&keys)[8]) {
    const uint8_t released[8] = {};
    if (!notify(keyboard, keyboardBit, keys, sizeof(keys))) return false;
    delay(18);
    bool ok = notify(keyboard, keyboardBit, released, sizeof(released));
    delay(10);
    return ok;
}

bool glyph(char c, uint8_t& mods, uint8_t& key) {
    mods = 0;
    if (c >= 'a' && c <= 'z') key = 4 + c - 'a';
    else if (c >= 'A' && c <= 'Z') { key = 4 + c - 'A'; mods = 2; }
    else if (c >= '1' && c <= '9') key = 0x1e + c - '1';
    else if (c == '0') key = 0x27;
    else if (c == ' ') key = 0x2c;
    else {
        constexpr char plain[] = "-=[]\\;'`,./";
        constexpr char shift[] = "_+{}|:\"~<>?";
        constexpr char symbols[] = "!@#$%^&*()";
        constexpr uint8_t codes[] = {0x2d,0x2e,0x2f,0x30,0x31,0x33,0x34,0x35,0x36,0x37,0x38};
        if (!c) return false;
        if (const char* p = strchr(plain, c)) key = codes[p - plain];
        else if (const char* p = strchr(shift, c)) { key = codes[p - shift]; mods = 2; }
        else if (const char* p = strchr(symbols, c)) { key = 0x1e + (p - symbols); mods = 2; }
        else return false;
    }
    return true;
}

bool combo(String arg, uint8_t (&keys)[8]) {
    int next = 2;
    for (;;) {
        int sep = arg.indexOf('+');
        String k = sep < 0 ? arg : arg.substring(0, sep);
        if (k == "ctrl") keys[0] |= 1;
        else if (k == "shift") keys[0] |= 2;
        else if (k == "alt") keys[0] |= 4;
        else if (k == "cmd") keys[0] |= 8;
        else {
            if (next == 8) return false;
            uint8_t key = 0, mods = 0;
            if (k == "enter") key = 0x28;
            else if (k == "esc") key = 0x29;
            else if (k == "backspace") key = 0x2a;
            else if (k == "tab") key = 0x2b;
            else if (k == "space") key = 0x2c;
            else if (k == "right") key = 0x4f;
            else if (k == "left") key = 0x50;
            else if (k == "down") key = 0x51;
            else if (k == "up") key = 0x52;
            else if (k == "delete") key = 0x4c;
            else if (k == "home") key = 0x4a;
            else if (k == "end") key = 0x4d;
            else if (k == "pageup") key = 0x4b;
            else if (k == "pagedown") key = 0x4e;
            else if (k.startsWith("f") && k.substring(1).toInt() >= 1 && k.substring(1).toInt() <= 12 &&
                     k == "f" + String(k.substring(1).toInt())) key = 0x39 + k.substring(1).toInt();
            else if (k.length() != 1 || !glyph(k[0], mods, key)) return false;
            keys[0] |= mods;
            keys[next++] = key;
        }
        if (sep < 0) return true;
        arg.remove(0, sep + 1);
    }
}

String run(String line) {
    int sep = line.indexOf(' ');
    String op = sep < 0 ? line : line.substring(0, sep), arg = sep < 0 ? "" : line.substring(sep + 1);
    if (op == "status") {
        char out[220];
        snprintf(out, sizeof(out), "ok {\"fw\":\"mhxy-hid-2\",\"input\":\"ble-hid\",\"usb\":\"serial-only\","
                 "\"paired\":%s,\"pairing\":%s,\"hid\":\"%s\",\"keyboard\":%s,\"mouse\":%s}",
                 bonded ? "true" : "false", pairing() ? "true" : "false",
                 ready(keyboardBit | mouseBit) ? "ready" : "disconnected",
                 ready(keyboardBit) ? "true" : "false", ready(mouseBit) ? "true" : "false");
        return out;
    }
    if (op == "ping") return "ok";
    if (op == "pair") {
        if (bonded) return "err already paired";
        pairAt = millis() | 1;
        return "ok 180";
    }
    const char* failed = "err Bluetooth HID report could not be sent";
    if (op == "key") {
        uint8_t keys[8] = {};
        if (!combo(arg, keys)) return "err unknown key";
        return stroke(keys) ? "ok" : failed;
    }
    if (op == "type") {
        if (arg.length() > 256) return "err text too long";
        for (char c : arg) { uint8_t mods, key; if (!glyph(c, mods, key)) return "err use printable ASCII"; }
        for (char c : arg) {
            uint8_t keys[8] = {};
            glyph(c, keys[0], keys[2]);
            if (!stroke(keys)) return failed;
        }
        return "ok";
    }
    if (op == "homescreen" || op == "media") {
        uint16_t usage = op == "homescreen" ? 0x223 : arg == "play-pause" ? 0xcd :
                         arg == "next" ? 0xb5 : arg == "previous" ? 0xb6 :
                         arg == "mute" ? 0xe2 : arg == "volume-up" ? 0xe9 :
                         arg == "volume-down" ? 0xea : 0;
        if (!usage) return "err unknown media key";
        const uint8_t report[] = {uint8_t(usage), uint8_t(usage >> 8)}, released[] = {0, 0};
        if (!notify(consumer, consumerBit, report, sizeof(report))) return failed;
        delay(40);
        return notify(consumer, consumerBit, released, sizeof(released)) ? "ok" : failed;
    }
    if (op == "release-all") {
        buttons = 0;
        if (ready(keyboardBit)) { const uint8_t keys[8] = {}; notify(keyboard, keyboardBit, keys, sizeof(keys)); }
        return ready(mouseBit) && sendMouse() ? "ok" : failed;
    }
    if (!ready(mouseBit)) return "err pair and reconnect the iPhone mouse";
    if (op == "home") {
        if (buttons) return "err release the mouse button before homing";
        for (int axis = 0; axis < 2; ++axis) {
            for (int i = 0; i < 14; ++i) {
                if (!(axis ? sendMouse(0, -127) : sendMouse(-127, 0))) return failed;
                delay(reportDelay);
            }
        }
        return "ok";
    }
    if (op == "press" || op == "release" || op == "click" || op == "doubleclick") {
        const uint8_t button = !arg.length() || arg == "left" ? 1 : arg == "right" ? 2 : arg == "middle" ? 4 : 0;
        if (!button) return "err use left, right, or middle";
        if (op == "release") { buttons &= ~button; return sendMouse() ? "ok" : failed; }
        for (int i = 0, times = op == "doubleclick" ? 2 : 1; i < times; ++i) {
            buttons |= button;
            heldAt = millis();
            if (!sendMouse()) { buttons &= ~button; return failed; }
            if (op == "press") return "ok";
            delay(60);
            buttons &= ~button;
            if (!sendMouse()) return failed;
            if (i + 1 < times) delay(75);
        }
        return "ok";
    }
    long x = 0, y = 0, step = 10;
    char extra;
    int n = sscanf(arg.c_str(), "%ld %ld %ld %c", &x, &y, &step, &extra);
    if (op == "scroll") {
        if (n < 1 || n > 2 || x < -127 || x > 127 || y < -127 || y > 127) return "err scroll range is -127..127";
        return sendMouse(0, 0, x, y) ? "ok" : failed;
    }
    if (op == "move") {
        if ((n != 2 && n != 3) || step < 1 || step > 40 || x < -2048 || x > 2048 || y < -2048 || y > 2048)
            return "err invalid relative movement";
        const int distance = lroundf(hypotf(x, y));
        const int reports = (distance + step - 1) / step;
        if (reports > 150) return "err move needs more than 150 reports";
        if (!reports) return "ok";
        AxisSteps horizontal(x, reports), vertical(y, reports);
        if (buttons) heldAt = millis();
        for (int i = 0; i < reports; ++i) {
            if (!sendMouse(horizontal.next(), vertical.next())) return failed;
            delay(reportDelay);
        }
        return "ok";
    }
    return "err unknown command";
}
}  // namespace

void setup() {
    Serial.begin(115200);
    if (!prefs.begin("mhxy-hid", false)) return;
    uint8_t addr[6];
    if (prefs.getBytesLength("address") != sizeof(addr) || prefs.getBytes("address", addr, sizeof(addr)) != sizeof(addr)) {
        esp_fill_random(addr, sizeof(addr));
        addr[5] |= 0xc0;
        if (prefs.putBytes("address", addr, sizeof(addr)) != sizeof(addr)) return;
    }
    NimBLEDevice::init(name);
    if (!NimBLEDevice::setOwnAddr(addr) || !NimBLEDevice::setOwnAddrType(BLE_OWN_ADDR_RANDOM)) return;
    NimBLEDevice::setSecurityAuth(true, false, true);
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
    NimBLEDevice::setSecurityInitKey(BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID);
    NimBLEDevice::setSecurityRespKey(BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID);
    String peer = prefs.getString("peer");
    uint8_t peerType = prefs.getUChar("peerType", 0xff);
    if (peer.length() == 17 && peerType <= 1) {
        trusted = NimBLEAddress(std::string(peer.c_str()), peerType);
        bonded = NimBLEDevice::isBonded(trusted);
    }
    auto* server = NimBLEDevice::createServer();
    server->setCallbacks(&peerEvents, false);
    server->advertiseOnDisconnect(false);
    auto* hid = new NimBLEHIDDevice(server);
    hid->setHidInfo(0, 0x02);
    hid->setPnp(0x02, 0x303a, 0x0000, 0x0100);
    hid->setReportMap(reportMap, sizeof(reportMap));
    hid->setBatteryLevel(100);
    keyboard = hid->getInputReport(1);
    hid->getOutputReport(1);
    mouse = hid->getInputReport(2);
    consumer = hid->getInputReport(3);
    for (auto* chr : {keyboard, mouse, consumer}) chr->setCallbacks(&reportEvents);
    server->start();
    advertising = NimBLEDevice::getAdvertising();
    advertising->setName(name);
    advertising->setAppearance(GENERIC_HID);
    advertising->addServiceUUID(hid->getHidService()->getUUID());
}

void loop() {
    if (advertising) {
        if (connection == noConnection) {
            bool shouldAdvertise = bonded || pairing();
            if (shouldAdvertise && !advertising->isAdvertising()) advertising->start();
            else if (!shouldAdvertise && advertising->isAdvertising()) advertising->stop();
        } else if (!authorized && millis() - connectAt > (pairing() ? pairTime : 12000)) {
            NimBLEDevice::getServer()->disconnect(connection);
        }
    }
    if (stateGeneration != linkGeneration) { stateGeneration = linkGeneration; buttons = 0; }
    if (buttons && millis() - heldAt > 5000) { buttons = 0; sendMouse(); }

    static String line;
    static bool overflow = false;
    for (int count = 0; count < 96 && Serial.available(); ++count) {
        int c = Serial.read();
        if (c == '\n') {
            int sep = line.indexOf('\t');
            if (!overflow && line.startsWith("MH2:") && sep >= 5 && sep <= 10 &&
                    line.substring(4, sep).toInt() > 0) {
                String id = line.substring(0, sep);
                line.remove(0, sep + 1);
                if (line.endsWith("\r")) line.remove(line.length() - 1);
                Serial.println(id + "\t" + (advertising ? run(line) : "err Bluetooth initialization failed"));
            }
            line = "";
            overflow = false;
        } else if (!overflow && c >= 0) {
            if (line.length() < 290) line += char(c);
            else overflow = true;
        }
    }
    delay(2);
}
