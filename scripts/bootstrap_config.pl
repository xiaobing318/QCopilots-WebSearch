use strict;
use warnings;
use utf8 ();
BEGIN { delete $ENV{PERL_JSON_PP_USE_B}; }
use JSON::PP ();

# Only the fields that govern Python installation are checked here. The complete
# schema is checked by pipeline.py once Python and its pinned validator exist.
my $default = shift @ARGV;
defined $default or die "Missing default configuration path\n";
my %value = ('--ConfigPath' => $default, '--Profile' => undef,
             '--Stages' => 'doctor,configure,build,test');
my ($check, $dry) = (0, 0);
for (my $i = 0; $i < @ARGV; ++$i) {
    my $arg = $ARGV[$i];
    if ($arg eq '--CheckOnly') { $check = 1; next; }
    if ($arg eq '--DryRun') { $dry = 1; next; }
    my ($name, $v) = split /=/, $arg, 2;
    exists $value{$name} or die "Unknown option: $name\n";
    if (!defined $v) {
        ++$i;
        $i < @ARGV && $ARGV[$i] !~ /^--/ or die "Missing value for $name\n";
        $v = $ARGV[$i];
    }
    $value{$name} = $v;
}
my $profile = $value{'--Profile'};
defined($profile) && length($profile) or die "--Profile is required\n";
utf8::decode($profile) or die "Profile name must use UTF-8\n";
my %allowed = map { $_ => 1 } qw(doctor bootstrap configure build test install publish);
my (%seen, $bootstrap);
for my $stage (split /,/, $value{'--Stages'}, -1) {
    $allowed{$stage} or die "Unknown stage: $stage\n";
    !$seen{$stage}++ or die "Duplicate stage: $stage\n";
    $bootstrap = 1 if $stage eq 'bootstrap';
}
open my $fh, '<:raw', $value{'--ConfigPath'} or die "Cannot open configuration\n";
-f $fh or die "Configuration must be a regular file\n";
my $raw = '';
while (1) {
    my $n = read($fh, my $chunk, 65536);
    defined $n or die "Cannot read configuration\n";
    last if !$n;
    $raw .= $chunk;
    length($raw) <= 1048576 or die "Configuration exceeds 1 MiB\n";
}
close $fh or die "Cannot close configuration\n";
$raw =~ s/\A\xEF\xBB\xBF//;
index($raw, "\0") < 0 or die "Configuration must use UTF-8\n";
utf8::decode($raw) or die "Configuration must use UTF-8\n";
my $codec = JSON::PP->new->max_depth(64)->max_size(1048576);
my $doc = $codec->decode($raw);
ref($doc) eq 'HASH' or die "Configuration must be an object\n";
for my $key (keys %$doc) {
    $key eq 'version' || $key eq 'profiles' or die "Unknown root field\n";
}
$codec->encode([$doc->{version}]) eq '[1]' or die "Unsupported configuration version\n";
ref($doc->{profiles}) eq 'HASH' or die "profiles must be an object\n";
my $p = $doc->{profiles}{$profile};
ref($p) eq 'HASH' or die "Selected profile does not exist\n";
my %fields = map { $_ => 1 } qw(platform qtRoot buildDir installDir cacheDir buildType
                                proxy jobs cmakeArgs packageGenerators offline tools);
for my $key (keys %$p) { $fields{$key} or die "Unknown profile field\n"; }
($p->{platform} // '') eq 'ubuntu-x64' or die "Python bootstrap requires ubuntu-x64\n";
my $offline = exists($p->{offline}) ? $p->{offline} : JSON::PP::false();
JSON::PP::is_bool($offline) or die "offline must be a JSON boolean\n";
my $proxy = $p->{proxy};
$codec->encode([$proxy]) =~ /\A\["/ or die "proxy must be a JSON string\n";
$proxy =~ /\A(?:|https?:\/\/[^\s\@]+)\z/ && $proxy !~ /[\x00-\x1f\x7f]/
    or die "proxy must be empty or an HTTP(S) URL without credentials\n";
utf8::encode($proxy);
binmode STDOUT, ':raw';
print join("\0", $bootstrap ? 1 : 0, $offline ? 1 : 0, $check, $dry, $proxy), "\0";
