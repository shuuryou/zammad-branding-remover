# Compile-checks .eco and .coffee files with Zammad's own gems.
# usage: zammad run bundle exec ruby validate_eco_coffee.rb FILE...
require 'eco'
require 'coffee-script'

bad = 0
ARGV.each do |f|
  src = File.read(f)
  begin
    if f.end_with?('.eco')
      Eco.compile(src)
    elsif f.end_with?('.coffee')
      CoffeeScript.compile(src)
    end
  rescue StandardError => e
    puts "INVALID #{f}: #{e.message.lines.first&.strip}"
    bad += 1
  end
end
exit(bad.zero? ? 0 : 1)
