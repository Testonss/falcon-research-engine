import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import "components"
import "pages"

ApplicationWindow {
    id: root
    visible: true
    title: "Falcon Terminal · Live Market"
    // Fallback size only: Python sets screen-relative geometry pre-show.
    width: 1280
    height: 800
    minimumWidth: 1100
    minimumHeight: 700
    color: FalconTheme.bgPrimary

    property var navItems: [
        { id: "meme", label: "Meme Radar" },
        { id: "radar", label: "Radar" },
        { id: "assets", label: "Assets" },
        { id: "signals", label: "Signals" },
        { id: "paper", label: "Paper" },
        { id: "reports", label: "Reports" },
        { id: "settings", label: "Settings" }
    ]
    property var placeholderText: {
        "paper": "Paper sessions arrive with the forward-research engine.",
        "reports": "Campaign reports stay in the Falcon CLI for now.",
        "settings": "Settings arrive with the next terminal milestone."
    }
    property string pageTitle: nav.page === "radar" ? "Live Market" : (nav.page === "signals" ? "Signal Workstation" : (nav.page === "meme" ? "Meme Coin Radar" : nav.page))
    // Responsive breakpoints: compact gutters/sidebar/panels under 1400px.
    property bool narrowMode: width < 1400
    property int navItemWidth: (narrowMode ? 120 : 150) - 24
    property int pageMargins: narrowMode ? 12 : 16
    property int sidePanelWidth: narrowMode ? 300 : 360
    property int bottomPanelHeight: narrowMode ? 130 : 150

    Timer {
        id: clock
        interval: 1000
        running: true
        repeat: true
        property string now: "--:--:-- UTC"
        onTriggered: {
            var d = new Date();
            now = ("0" + d.getUTCHours()).slice(-2) + ":" + ("0" + d.getUTCMinutes()).slice(-2) + ":" + ("0" + d.getUTCSeconds()).slice(-2) + " UTC";
        }
        Component.onCompleted: triggered()
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        // Sidebar
        Rectangle {
            Layout.preferredWidth: root.narrowMode ? 120 : 150
            Layout.minimumWidth: 110
            Layout.maximumWidth: 150
            Layout.fillHeight: true
            color: FalconTheme.bgSecondary
            border.color: FalconTheme.borderSubtle
            border.width: 1
            ColumnLayout {
                anchors.fill: parent
                anchors.margins: FalconTheme.s12
                spacing: FalconTheme.s4
                Repeater {
                    model: root.navItems
                    Rectangle {
                        property bool active: modelData.id === nav.page
                        property bool hovered: navMouse.containsMouse
                        property bool pressed: navMouse.pressed
                        Layout.fillWidth: true
                        Layout.minimumWidth: root.navItemWidth
                        Layout.preferredWidth: root.navItemWidth
                        Layout.maximumWidth: root.navItemWidth
                        Layout.preferredHeight: 38
                        radius: FalconTheme.r6
                        color: active ? "#2a2118" : (pressed ? FalconTheme.bgSelected : (hovered ? FalconTheme.bgHover : "transparent"))
                        border.color: active ? FalconTheme.accentGold : (hovered ? FalconTheme.borderActive : "transparent")
                        border.width: 1
                        opacity: (modelData.id === "radar" || modelData.id === "signals" || modelData.id === "meme" || modelData.id === "assets") ? 1.0 : 0.55
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.left: parent.left
                            anchors.leftMargin: FalconTheme.s12
                            text: modelData.label
                            color: active ? FalconTheme.accentGold : FalconTheme.textSecondary
                            font.family: FalconTheme.fontFamily
                            font.pixelSize: FalconTheme.body
                            font.bold: active
                        }
                        MouseArea {
                            id: navMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: nav.go(modelData.id)
                        }
                    }
                }
                Item { Layout.fillHeight: true }
                Text {
                    text: "FALCON TERMINAL"
                    color: FalconTheme.textMuted
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.micro
                    font.bold: true
                }
                Text {
                    text: "v0.2 prototype · read-only"
                    color: FalconTheme.textMuted
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.micro
                }
            }
        }

        // Main column
        ColumnLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            // Top bar
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 56
                color: FalconTheme.bgSecondary
                border.color: FalconTheme.borderSubtle
                border.width: 1
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: FalconTheme.s20
                    anchors.rightMargin: FalconTheme.s20
                    spacing: FalconTheme.s12
                    Text { text: "FALCON TERMINAL"; color: FalconTheme.accentGold; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.panelTitle; font.bold: true }
                    Text { text: root.pageTitle; color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.body }
                    Item { Layout.fillWidth: true }
                    Rectangle { width: 8; height: 8; radius: 4; color: health.state === "CONNECTED" ? FalconTheme.positive : (health.state === "STALE" ? FalconTheme.warning : (health.state === "RECONNECTING" ? FalconTheme.warning : (health.state === "ERROR" ? FalconTheme.negative : FalconTheme.textMuted))) }
                    Text { text: health.state === "CONNECTED" ? "Connected" : (health.state === "STALE" ? "Stale" : (health.state === "RECONNECTING" ? "Reconnecting" : (health.state === "ERROR" ? "Error" : "Connecting"))); color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    Text { text: health.detail; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    Text { text: clock.now; color: FalconTheme.textSecondary; font.family: "Consolas"; font.pixelSize: FalconTheme.small }
                }
            }

            // Pages
            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: root.pageMargins
                currentIndex: nav.page === "radar" ? 0 : (nav.page === "signals" ? 1 : (nav.page === "meme" ? 2 : (nav.page === "assets" ? 3 : 4)))
                RadarPage {}
                SignalsPage {}
                MemeRadarPage {}
                AssetsPage {}
                Panel {
                    objectName: "placeholderPanel"
                    title: "Coming with a later milestone"
                    EmptyState {
                        anchors.fill: parent
                        title: ""
                        body: root.placeholderText[nav.page] || "Not in this prototype."
                    }
                }
            }
        }
    }
}
