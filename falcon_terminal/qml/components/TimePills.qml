import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: root
    property var intervals: ["1m", "5m", "15m", "1h", "4h", "1D"]
    property string active: "1m"
    property var supported: ["1m"]
    spacing: FalconTheme.s4
    RowLayout {
        spacing: FalconTheme.s4
        Repeater {
            model: root.intervals
            Rectangle {
                property bool isActive: modelData === root.active
                property bool ok: root.supported.indexOf(modelData) >= 0
                Layout.preferredWidth: 40
                Layout.preferredHeight: 26
                radius: FalconTheme.r4
                color: isActive ? FalconTheme.accentGold : FalconTheme.bgPanelAlt
                border.color: isActive ? FalconTheme.accentGold : FalconTheme.borderDefault
                opacity: ok ? 1.0 : 0.45
                Text {
                    anchors.centerIn: parent
                    text: modelData
                    color: isActive ? "#0a0d13" : FalconTheme.textSecondary
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.small
                    font.bold: isActive
                }
            }
        }
    }
    Text {
        visible: root.supported.indexOf(root.active) < 0
        text: "interval not supported by this feed"
        color: FalconTheme.textMuted
        font.family: FalconTheme.fontFamily
        font.pixelSize: FalconTheme.micro
    }
}
