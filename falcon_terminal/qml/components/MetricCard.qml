import QtQuick
import QtQuick.Layouts

Rectangle {
    property string label: ""
    property string value: "—"
    property color valueColor: FalconTheme.textPrimary
    color: FalconTheme.bgPanelAlt
    border.color: FalconTheme.borderSubtle
    border.width: 1
    radius: FalconTheme.r6
    implicitHeight: 62

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: FalconTheme.s12
        spacing: 2
        Text {
            text: label
            color: FalconTheme.textMuted
            font.family: FalconTheme.fontFamily
            font.pixelSize: FalconTheme.micro
            font.capitalization: Font.AllUppercase
        }
        Text {
            text: value
            color: valueColor
            font.family: FalconTheme.fontFamily
            font.pixelSize: FalconTheme.metricValue
            font.bold: true
        }
    }
}
