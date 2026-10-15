import QtQuick

Column {
    id: root
    property string label: ""
    property string value: "—"
    property color valueColor: FalconTheme.textPrimary
    property int valueSize: FalconTheme.metricValue
    spacing: 2
    Text {
        text: root.label.toUpperCase()
        color: FalconTheme.textMuted
        font.family: FalconTheme.fontFamily
        font.pixelSize: FalconTheme.micro
    }
    Text {
        text: root.value
        color: root.valueColor
        font.family: FalconTheme.fontFamily
        font.pixelSize: root.valueSize
        font.bold: true
    }
}
