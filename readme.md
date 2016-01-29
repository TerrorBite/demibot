## Demibot IRC bot

This is a modular IRC bot, written in Python, built on top of the `ircstack` IRC library. This library is also being developed in this repository.

Demibot is deliberately written from scratch from the bottom up in order to maintain full control of the entire IRC stack. This allows advanced features such as IRCv3 support to be included, where other IRC libraries may lack support for these features. The library that has been developed for this purpose, `ircstack`, aims for maximum compatibility with various flavors of IRCd, by parsing ISUPPORT headers and abstracting away raw IRC commands as much as possible.

*Note: demibot is not related to [mikar/demibot](//github.com/mikar/demibot).*
