import QtQuick
import QtQuick.Layouts

ColumnLayout {
    id: root
    property var intervals: ["1m", "5m", "15m", "1h", "4h", "1D"]
    property string active: "1m"
    property var supported: ["1m"]
    signal pick(string timeframe)
    spacing: FalconTheme.s4
    RowLayout {
        spacing: FalconTheme.s4
        Repeater {
            model: root.intervals
            Rectangle {
                property bool isActive: modelData === root.active
                property bool ok: root.supported.indexOf(modelData) >= 0
                property bool hovered: pillMouse.containsMouse
                property bool pressed: pillMouse.pressed
                Layout.preferredWidth: 40
                Layout.preferredHeight: 26
                radius: FalconTheme.r4
                color: isActive ? FalconTheme.accentGold : (pressed && ok ? FalconTheme.bgSelected : (hovered && ok ? FalconTheme.bgHover : FalconTheme.bgPanelAlt))
                border.color: isActive ? FalconTheme.accentGold : (hovered && ok ? FalconTheme.borderActive : FalconTheme.borderDefault)
                opacity: ok ? 1.0 : 0.45
                Text {
                    anchors.centerIn: parent
                    text: modelData
                    color: isActive ? "#0a0d13" : FalconTheme.textSecondary
                    font.family: FalconTheme.fontFamily
                    font.pixelSize: FalconTheme.small
                    font.bold: isActive
                }
                MouseArea {
                    id: pillMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    enabled: parent.ok
                    cursorShape: parent.ok ? Qt.PointingHandCursor : Qt.ArrowCursor
                    onClicked: root.pick(modelData)
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
