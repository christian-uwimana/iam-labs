# Compares two copies of an app's provisioning schema (copied from the schema editor) and lists only what
# matters: attributes, attribute mappings and rule metadata. Version stamps and layout are ignored.
# Usage: .\Compare-Schema.ps1 .\schema-original.json .\schema-customer.json | Out-String -Width 250
param([string]$Before, [string]$After)

function Get-SchemaTable([string]$Path) {
  $s = Get-Content $Path -Raw | ConvertFrom-Json
  $t = @{}
  foreach ($d in $s.directories) { foreach ($o in $d.objects) { foreach ($x in $o.attributes) {
    $t["attribute | $($d.name) | $($o.name) | $($x.name)"] = "type=$($x.type); required=$($x.required)" } } }
  foreach ($r in $s.synchronizationRules) {
    foreach ($om in $r.objectMappings) { foreach ($m in $om.attributeMappings) {
      $t["mapping | $($om.name) | $($m.targetAttributeName)"] = "source=$($m.source.expression); matching=$($m.matchingPriority)" } }
    foreach ($md in $r.metadata) {
      $v = [string]$md.value
      $old = ([regex]::Matches($v, '"userPrincipalName"')).Count
      $new = ([regex]::Matches($v, '"onPremisesUserPrincipalName"')).Count
      $t["metadata | $($md.key)"] = "userPrincipalName x$old; onPremisesUserPrincipalName x$new" } }
  return $t
}

$a = Get-SchemaTable $Before
$b = Get-SchemaTable $After
$diff = @(@($a.Keys) + @($b.Keys) | Sort-Object -Unique | Where-Object { $a[$_] -ne $b[$_] })
if ($diff.Count -eq 0) { 'No differences in attributes, mappings or rule metadata.' }
else { $diff | ForEach-Object { [pscustomobject]@{ Item = $_; Before = $a[$_]; After = $b[$_] } } | Format-Table -AutoSize -Wrap }
