# Connecting middleware to webhook listeners

# Introduction

This article showcases a design pattern that opens up many possibilities to create engaging and useful microapp integrations, namely the concept of combining middleware with webhook listeners. It will cover first a high-level overview of the design pattern arcitecture and then go into further detail using a real example implementation.

# Architecture

![Middlware Architecture](/architecture.png)

The schematic above shows how middleware can be used to act as an interface between the microapp integration running on the Microapp Platform and the System of Record (SoR).

-  The starting point in the architecture shown is a data loading endpoint that sends an http request to the middleware.
-  This triggers the middleware to create subscriptions to a set list of events using the webhook API of your chosen SoR.
-  The subscriptions are configured such that the events are received by middlware.
-  When an event occurs, the middleware is triggered to handle the event. This means sending any ancillary calls to the SoR to get the full context of the event.
-  Once this is done, the relevant information can be sent to the Microapp Platform to be received by a webhook listener in the integration.
-  The webhook listener places the data it receives in a cache where it where it triggers a notification to be sent to the end user.
-  Any subsequent actions taken by the end user as a result of the notification, need not be modified and can be passed straight through to the SoR.

This is a high level overview but there is room for a number of different design decisions when implementing a middleware layer. For example, where should you host your middleware AWS, Azure or Google Cloud or other? Should you implement the middleware functionality as serverless functions or a web application? What web framework is the flavour of the month? The next section goes into some detail describing one possible implementation using o365 Outlook as the SoR.

# Example o365 Implementation

## Functionality and Design Choices

For demonstration purposes we have built a microapp integration and accomonpying middleware which implements the following features:

**Notifications**
1.  Emails with high priority
1.  Emails from your boss
1.  Calendar invites

**Service Actions**
1.  View notification emails via a deep link to Outlook
1.  Respond to calendar invites (attending/tentative/declined)

To implement the middleware itself we chose to write a web application using the Flask framework hosted on Microsoft Azure. Flask has the advantage of being portable to a number of different cloud hosting solutions and Azure allows for easy setup and teardown of cloud host containers via the command line.


