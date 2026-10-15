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
        "assets": "Asset book arrives with portfolio tracking.",
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
                        Layout.fillWidth: true
                        Layout.minimumWidth: root.navItemWidth
                        Layout.preferredWidth: root.navItemWidth
                        Layout.maximumWidth: root.navItemWidth
                        Layout.preferredHeight: 38
                        radius: FalconTheme.r6
                        color: active ? "#2a2118" : "transparent"
                        border.color: active ? FalconTheme.accentGold : "transparent"
                        border.width: 1
                        opacity: (modelData.id === "radar" || modelData.id === "signals" || modelData.id === "meme") ? 1.0 : 0.55
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
                            anchors.fill: parent
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
                    Rectangle { width: 8; height: 8; radius: 4; color: health.connected ? (health.stale ? FalconTheme.warning : FalconTheme.positive) : FalconTheme.negative }
                    Text { text: health.connected ? (health.stale ? "Stale" : "Connected") : "Offline"; color: FalconTheme.textSecondary; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    Text { text: health.detail; color: FalconTheme.textMuted; font.family: FalconTheme.fontFamily; font.pixelSize: FalconTheme.small }
                    Text { text: clock.now; color: FalconTheme.textSecondary; font.family: "Consolas"; font.pixelSize: FalconTheme.small }
                }
            }

            // Pages
            StackLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: root.pageMargins
                currentIndex: nav.page === "radar" ? 0 : (nav.page === "signals" ? 1 : (nav.page === "meme" ? 2 : 3))
                RadarPage {}
                SignalsPage {}
                MemeRadarPage {}
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
